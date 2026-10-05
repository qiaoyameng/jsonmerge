# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.jsonvalue import JSONValue, text_type
import heapq
import json
import jsonschema
import re

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

    def get_key(self, walk, item, idRef):
        return self._resolve_ref(walk, item, idRef)

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            yield i, key, item

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None,
            sortReverse=None, beforeAfter=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        if beforeAfter is not None and sortByRef is not None:
            raise SchemaError("'beforeAfter' and 'sortByRef' merge options cannot be used together",
                    schema.get('mergeOptions'))

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

        if beforeAfter is not None:
            base = self.order_by_relations(walk, base, schema, idRef, beforeAfter)
        else:
            self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def _key_token(self, key):
        """Hashable, collision-free token for an item ID."""
        if isinstance(key, list):
            return ('c', tuple(self._key_token(k) for k in key))
        elif isinstance(key, dict):
            return ('o', tuple(sorted(
                (self._key_token(k), self._key_token(v)) for k, v in key.items())))
        else:
            return ('s', key)

    def _format_ref_id(self, ref_id):
        if isinstance(ref_id, text_type):
            return ref_id
        return json.dumps(ref_id, ensure_ascii=False)

    @staticmethod
    def _is_scalar_ref_id(value):
        return value is not None and isinstance(value, (text_type, int, float, bool))

    def _validate_before_after(self, schema, before_after):
        options = schema.get('mergeOptions')

        if not isinstance(before_after, dict):
            raise SchemaError("'beforeAfter' merge option must be an object containing "
                    "'before' and/or 'after' mappings", options)

        for direction in ('before', 'after'):
            mapping = before_after.get(direction)
            if mapping is None:
                continue

            if not isinstance(mapping, dict):
                raise SchemaError("'%s' mapping in 'beforeAfter' merge option must be an object"
                        % (direction,), options)

            for ref_id, targets in mapping.items():
                if not self._is_scalar_ref_id(ref_id):
                    raise SchemaError("Keys in '%s' mapping in 'beforeAfter' merge option must be "
                            "JSON string, number or boolean item IDs" % (direction,), options)

                if not isinstance(targets, list):
                    raise SchemaError("Values in '%s' mapping in 'beforeAfter' merge option must "
                            "be JSON string, number or boolean IDs or lists of such IDs"
                            % (direction,), options)

                for target in targets:
                    if not self._is_scalar_ref_id(target):
                        raise SchemaError("Values in '%s' mapping in 'beforeAfter' merge option must "
                                "be JSON string, number or boolean IDs or lists of such IDs"
                                % (direction,), options)

        unknown = set(before_after) - set(('before', 'after'))
        if unknown:
            raise SchemaError("Unknown keys in 'beforeAfter' merge option: %s"
                    % (', '.join(sorted(unknown)),), options)

    def order_by_relations(self, walk, base, schema, idRef, before_after):
        """Reorder the merged array so that all declared relations hold.

        before_after is an object with optional 'before' and 'after'
        mappings, each mapping an item ID to a list of IDs.  In a 'before'
        mapping the key item must precede each listed item; in an 'after'
        mapping it must follow them.

        Items not constrained by any relation keep their stable position:
        items present in base come first (in base order), items newly
        appended from head come after them (in head order).
        """

        options = schema.get('mergeOptions')
        self._validate_before_after(schema, before_after)

        # Assign every item in the merged array its stable fallback position.
        index_of_token = {}
        for index, item in enumerate(base):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            index_of_token[self._key_token(key)] = index

        # Build the constraint graph: edge (src -> dst) means src precedes dst.
        edges = set()
        missing = []

        def resolve(ref_id):
            return index_of_token.get(self._key_token(ref_id))

        for direction, reverse in (('before', False), ('after', True)):
            mapping = before_after.get(direction)
            if mapping is None:
                continue

            for ref_id, targets in mapping.items():
                src = resolve(ref_id)
                if src is None:
                    missing.append(self._format_ref_id(ref_id))
                    continue

                if not isinstance(targets, list):
                    targets = [targets]

                for target in targets:
                    dst = resolve(target)
                    if dst is None:
                        missing.append(self._format_ref_id(target))
                        continue

                    if reverse:
                        edges.add((dst, src))
                    else:
                        edges.add((src, dst))

        if missing:
            raise SchemaError("'beforeAfter' merge option references item IDs that are not "
                    "present in the merged array: %s"
                    % (', '.join(sorted(set(missing))),), options)

        # Stable topological sort (Kahn's algorithm); ties are broken by the
        # stable fallback position, so unconstrained items retain base/head
        # first-appearance order.
        count = len(base.val)
        indegree = [0] * count
        successors = [[] for _ in range(count)]
        for src, dst in edges:
            indegree[dst] += 1
            successors[src].append(dst)

        heap = []
        for index, degree in enumerate(indegree):
            if degree == 0:
                heapq.heappush(heap, index)

        ordered = []
        while heap:
            src = heapq.heappop(heap)
            ordered.append(src)
            for dst in successors[src]:
                indegree[dst] -= 1
                if indegree[dst] == 0:
                    heapq.heappush(heap, dst)

        if len(ordered) != count:
            cycle = self._find_cycle(walk, base, idRef, indegree, successors)
            raise SchemaError("Relations in 'beforeAfter' merge option form a cycle: " + cycle,
                    options)

        return JSONValue([base[index].val for index in ordered], base.ref)

    def _find_cycle(self, walk, base, idRef, indegree, successors):
        # Kahn's algorithm leaves exactly the nodes belonging to cycles with
        # non-zero indegree.  Depth-first search from one such node within
        # that subgraph until we return to it to extract a single cycle.
        remaining = set(index for index, degree in enumerate(indegree)
                if degree > 0)

        def key_at(index):
            try:
                key = self.get_key(walk, base[index], idRef)
            except jsonschema.RefResolutionError:
                return None
            return self._format_ref_id(key)

        start = next(iter(remaining))
        stack = [start]
        on_stack = {start}

        while stack:
            current = stack[-1]
            for dst in successors[current]:
                if dst not in remaining:
                    continue
                if dst in on_stack:
                    # trim any prefix leading into the cycle so the
                    # reported edges close on themselves
                    cycle = stack[stack.index(dst):] + [dst]
                    return self._format_cycle(cycle, key_at)
                stack.append(dst)
                on_stack.add(dst)
                break
            else:
                finished = stack.pop()
                on_stack.discard(finished)
                remaining.discard(finished)

        # The remaining graph always contains a cycle after a failed
        # topological sort; this point is unreachable in practice.
        return self._format_cycle([], key_at)

    @staticmethod
    def _format_cycle(cycle_indices, key_at):
        parts = []
        for src, dst in zip(cycle_indices, cycle_indices[1:]):
            parts.append("%s before %s" % (key_at(src), key_at(dst)))
        return ' -> '.join(parts)

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
