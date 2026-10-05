# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError, \
                                 OrderingError
from jsonmerge.jsonvalue import JSONValue
import jsonschema
import re
import heapq

class Strategy(object):
    """Base class for merge strategies.
    """

    def merge(self, walk, base, head, schema, **kwargs):
        """Merge head instance into base.

        walk -- WalkInstance object for the current context.
        base -- JSONValue being merged into.
        head -- JSONValue being merged.
        schema -- Schema used for merging (also JSONValue)
        kwargs -- Dict with any extra options given in the 'mergeOptions'
        keyword

        Specific merge strategies should override this method to implement
        their behavior.

        The function should return the object resulting from the merge.

        Recursion into the next level, if necessary, is achieved by calling
        walk.descend() method.
        """
        raise NotImplemented

    def get_schema(self, walk, schema, **kwargs):
        """Return the schema for the merged document.

        walk -- WalkSchema object for the current context.
        schema -- Original document schema.
        kwargs -- Dict with any extra options given in the 'mergeOptions'
        keyword.

        Specific merge strategies should override this method to modify the
        document schema depending on the behavior of the merge() method.

        The function should return the schema for the object resulting from the
        merge.

        Recursion into the next level, if necessary, is achieved by calling
        walk.descend() method.
        """
        raise NotImplemented

    def _resolve_ref(self, walk, item, ref):
        if walk.is_type(JSONValue(ref), 'array'):
            resolved = [ walk.resolver.resolve_fragment(item.val, i) for i in ref ]
        else:
            resolved = walk.resolver.resolve_fragment(item.val, ref)

        return resolved

class Overwrite(Strategy):
    def merge(self, walk, base, head, schema, **kwargs):
        return head

    def get_schema(self, walk, schema, **kwargs):
        return schema

class Discard(Strategy):
    def merge(self, walk, base, head, schema, keepIfUndef=False, **kwargs):
        if base.is_undef() and keepIfUndef:
            return head
        else:
            return base

    def get_schema(self, walk, schema, **kwargs):
        return schema

class Version(Strategy):

    def add_metadata(self, head, metadata):
        if metadata is None:
            rv = dict()
        else:
            rv = dict(metadata)

        rv['value'] = head
        return rv

    def merge(self, walk, base, head, schema, limit=None, unique=None, ignoreDups=True, metadata=None, **kwargs):

        # backwards compatibility
        if unique is False:
            ignoreDups = False

        if metadata is not None:
            if not walk.is_type(JSONValue(val=metadata), "object"):
                raise SchemaError("'metadata' option does not contain an object")

        if base.is_undef():
            base = JSONValue(val=[], ref=base.ref)
            last_entry = JSONValue(undef=True)
        else:
            if not walk.is_type(base, "array"):
                raise BaseInstanceError("Base is not an array. "
                        "Base not previously generated with this strategy?", base)

            base = JSONValue(list(base.val), base.ref)

            if base.val:
                last_entry = base[-1]

                if not walk.is_type(last_entry, "object"):
                    raise BaseInstanceError("Last entry in the versioned array is not an object. "
                            "Base not previously generated with this strategy?", last_entry)

                if 'value' not in last_entry.val:
                    raise BaseInstanceError("Last entry in the versioned array has no 'value' property. "
                            "Base not previously generated with this strategy?", last_entry)
            else:
                last_entry = JSONValue(undef=True)

        if not ignoreDups or last_entry.is_undef() or last_entry['value'].val != head.val:
            base.val.append(self.add_metadata(head.val, metadata))
            if limit is not None:
                base.val = base.val[-limit:]

        return base

    def get_schema(self, walk, schema, limit=None, metadataSchema=None, **kwargs):

        if metadataSchema is not None:
            item = dict(walk.resolve_subschema_option_refs(metadataSchema))
        else:
            item = {}

        if 'properties' not in item:
            item['properties'] = {}
        else:
            item['properties'] = dict(item['properties'])

        item['properties']['value'] = schema.val

        rv = {  "type": "array",
                "items": item }

        if limit is not None:
            rv['maxItems'] = limit

        return JSONValue(rv, schema.ref)

class ArrayStrategy(Strategy):
    def merge(self, walk, base, head, schema, **kwargs):
        if not walk.is_type(head, "array"):
            raise HeadInstanceError("Head is not an array", head)

        if base.is_undef():
            base = JSONValue([], base.ref)
        else:
            if not walk.is_type(base, "array"):
                raise BaseInstanceError("Base is not an array", base)

            base = JSONValue(list(base.val), base.ref)

        return self._merge(walk, base, head, schema, **kwargs)

    def default_key(self):
        # This object always sorts after other items
        class UnknownKey:
            def __lt__(self, other):
                return False

            def __gt__(self, other):
                if isinstance(other, UnknownKey):
                    return False
                else:
                    return True

        return UnknownKey()

    def sort_array(self, walk, base, sortByRef, sortReverse):
        assert walk.is_type(base, "array")

        if sortByRef is None:
            return

        def key(item):
            try:
                return self._resolve_ref(walk, item, sortByRef)
            except jsonschema.RefResolutionError:
                return self.default_key()

        base.sort(key=key, reverse=bool(sortReverse))

class Append(ArrayStrategy):
    def _merge(self, walk, base, head, schema, sortByRef=None, sortReverse=None, **kwargs):
        base.val += head.val

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        schema.val.pop('maxItems', None)
        schema.val.pop('uniqueItems', None)

        return schema

class ArrayMergeById(ArrayStrategy):

    _MISSING = object()

    def get_key(self, walk, item, idRef):
        return self._resolve_ref(walk, item, idRef)

    def _format_id(self, key):
        if isinstance(key, str):
            return "'%s'" % (key,)
        else:
            return str(key)

    def _parse_ordering(self, ordering):
        """Normalize the 'ordering' option into a list of edges.

        Each edge is a (src, dst) pair expressing that the item identified
        by src must appear before the item identified by dst.
        """

        def as_id_list(value):
            if isinstance(value, list):
                return value
            else:
                return [value]

        edges = []

        if isinstance(ordering, list):
            for edge in ordering:
                if not isinstance(edge, dict):
                    raise SchemaError("'ordering' option must be a list of "
                            "objects with 'id' and 'before'/'after' keys")
                src = edge.get('id')
                for key in edge:
                    if key not in ('id', 'before', 'after'):
                        raise SchemaError("Unknown 'ordering' edge key '%s'"
                                % (key,))
                for dst in as_id_list(edge.get('before', [])):
                    edges.append((src, dst))
                for dst in as_id_list(edge.get('after', [])):
                    edges.append((dst, src))
        elif isinstance(ordering, dict):
            for src, relations in ordering.items():
                if not isinstance(relations, dict):
                    raise SchemaError("Relations for item '%s' in 'ordering' "
                            "option must be an object" % (src,))
                for key in relations:
                    if key not in ('before', 'after'):
                        raise SchemaError("Unknown 'ordering' relation key "
                                "'%s'" % (key,))
                for dst in as_id_list(relations.get('before', [])):
                    edges.append((src, dst))
                for dst in as_id_list(relations.get('after', [])):
                    edges.append((dst, src))
        else:
            raise SchemaError("'ordering' option must be an object mapping "
                    "item IDs to 'before'/'after' relations, or a list of "
                    "edges")

        return edges

    def _find_cycle(self, succ, n):
        """Return a list of node indices making up a cycle, or None."""

        state = [0] * n
        stack = []
        stack_index = [-1] * n

        def visit(u):
            state[u] = 1
            stack_index[u] = len(stack)
            stack.append(u)

            for v in succ[u]:
                if state[v] == 0:
                    cycle = visit(v)
                    if cycle is not None:
                        return cycle
                elif state[v] == 1:
                    return stack[stack_index[v]:] + [v]

            stack.pop()
            stack_index[u] = -1
            state[u] = 2
            return None

        for u in range(n):
            if state[u] == 0:
                cycle = visit(u)
                if cycle is not None:
                    return cycle

        return None

    def _apply_ordering(self, walk, base, idRef, ordering, base_keys):
        """Reorder the merged array to satisfy declared before/after edges.

        The relative order is a stable topological sort: among items that
        are simultaneously available, the item that appeared first in base
        is picked first, followed by items newly introduced by head.
        """

        edges = self._parse_ordering(ordering)

        nodes = list(range(len(base.val)))
        keys = []
        # Indices of the first occurrence of each ID. Matching is done by
        # equality so that compound (list-valued) IDs are supported.
        first_index = []

        for i, item in enumerate(base):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                key = self._MISSING
            keys.append(key)
            if key is not self._MISSING:
                if not any(key == keys[j] for j in first_index):
                    first_index.append(i)

        def from_base(key):
            return any(key == base_key for base_key in base_keys)

        def priority(i):
            key = keys[i]
            if key is self._MISSING or from_base(key):
                return (0, i)
            else:
                return (1, i)

        succ = [set() for _ in nodes]
        indegree = [0] * len(nodes)

        def resolve(key):
            idx = next((j for j in first_index if key == keys[j]), None)
            if idx is None:
                raise OrderingError("'ordering' option references "
                        "unknown item ID %s" % (self._format_id(key),),
                        JSONValue(undef=True))
            return idx

        for src, dst in edges:
            u = resolve(src)
            v = resolve(dst)
            if v not in succ[u]:
                succ[u].add(v)
                indegree[v] += 1

        n = len(nodes)
        available = [(priority(i), i) for i in nodes if indegree[i] == 0]
        heapq.heapify(available)

        ordered = []
        while available:
            _, u = heapq.heappop(available)
            ordered.append(u)
            for v in sorted(succ[u], key=priority):
                indegree[v] -= 1
                if indegree[v] == 0:
                    heapq.heappush(available, (priority(v), v))

        if len(ordered) != n:
            cycle = self._find_cycle(succ, n)
            cycle_ids = [self._format_id(keys[i]) for i in cycle]
            raise OrderingError("'ordering' relations contain a cycle: "
                    + " -> ".join(cycle_ids),
                    JSONValue(undef=True))

        base.val = [base.val[i] for i in ordered]

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            yield i, key, item

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None, sortReverse=None, ordering=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        base_keys = []
        for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):
            if not any(base_key == key for key in base_keys):
                base_keys.append(base_key)

        for i, key_1, item_1 in self.iter_index_key_item(walk, head, idRef):
            for j, key_2, item_2 in self.iter_index_key_item(walk, head, idRef):
                if j < i:
                    if key_1 == key_2:
                        raise HeadInstanceError("Id '%s' was not unique in head" % (key_1,), item_1)
                else:
                    break

        for i, head_key, head_item in self.iter_index_key_item(walk, head, idRef):

            if head_key == ignoreId:
                continue

            matching_j = []
            for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):

                if base_key == head_key:
                    matching_j.append(j)
                    matched_item = base_item

            if len(matching_j) == 1:
                # If there was exactly one match, we replace it with a merged item
                j = matching_j[0]
                base[j] = walk.descend(subschema, matched_item, head_item)
            elif len(matching_j) == 0:
                # If there wasn't a match, we append a new object
                base.append(walk.descend(subschema, JSONValue(undef=True), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError("Id '%s' was not unique in base" % (base_key,), base[j])

        if ordering is not None:
            self._apply_ordering(walk, base, idRef, ordering, base_keys)
        else:
            self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        subschema = schema.get('items')
        if not subschema.is_undef():
            schema['items'] = walk.descend(subschema)

        return schema


class ArrayMergeByIndex(ArrayMergeById):

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            yield i, i, item


class ObjectMerge(Strategy):
    """A Strategy for merging objects.

    Resulting objects have properties from both base and head. Any
    properties that are present both in base and head are merged based
    on the strategy specified further down in the hierarchy (e.g. in
    properties, patternProperties or additionalProperties schema
    keywords). 

    walk -- WalkInstance object for the current context.
    base -- JSONValue being merged into.
    head -- JSONValue being merged.
    schema -- Schema used for merging (also JSONValue)
    objclass_menu -- A dictionary of classes to use as a JSON object.
    kwargs -- Any extra options given in the 'mergeOptions' keyword.

    objclass_menu should be a dictionary that maps a string name to a function
    or class that will return an empty dictionary-like object to use as a JSON
    object.  The function must accept either no arguments or a dictionary-like
    object.  The name '_default' represents the default object to use if not
    overridden by the objClass option.

    One mergeOption is supported:

    objClass -- a name for the class to use as a JSON object in the output.
    """
    def merge(self, walk, base, head, schema, objclass_menu=None, objClass='_default', **kwargs):
        if not walk.is_type(head, "object"):
            raise HeadInstanceError("Head is not an object", head)

        if objclass_menu is None:
            objclass_menu = { '_default': dict }

        objcls = objclass_menu.get(objClass)
        if objcls is None:
            raise SchemaError("objClass '%s' not recognized" % objClass, schema)

        if base.is_undef():
            base = JSONValue(objcls(), base.ref)
        else:
            if not walk.is_type(base, "object"):
                raise BaseInstanceError("Base is not an object", base)

            base = JSONValue(objcls(base.val), base.ref)

        for k, v in head.items():

            subschema = JSONValue(undef=True)

            # get subschema for this element
            if not schema.is_undef():
                p = schema.get('properties')
                if not p.is_undef():
                    subschema = p.get(k)

                if subschema.is_undef():
                    p = schema.get('patternProperties')
                    if not p.is_undef():
                        for pattern, s in p.items():
                            if re.search(pattern, k):
                                subschema = s

                if subschema.is_undef():
                    p = schema.get('additionalProperties')
                    # additionalProperties can be boolean in draft 4
                    if not p.is_undef() and walk.is_type(p, "object"):
                        subschema = p

            base[k] = walk.descend(subschema, base.get(k), v)

        return base

    def get_schema(self, walk, schema, **kwargs):
        schema2 = JSONValue(dict(schema.val), schema.ref)

        def descend_keyword(keyword):
            p = schema.get(keyword)
            if not p.is_undef():
                for k, v in p.items():
                    schema2[keyword][k] = walk.descend(v)

        descend_keyword("properties")
        descend_keyword("patternProperties")

       # additionalProperties can be boolean in draft 4
        p = schema.get("additionalProperties")
        if not p.is_undef() and walk.is_type(p, "object"):
            schema2["additionalProperties"] = walk.descend(p)

        return schema2
