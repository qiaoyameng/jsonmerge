# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.constraints import OrderConstraints
from jsonmerge.jsonvalue import JSONValue
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
               sortReverse=None, orderConstraints=False, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        if orderConstraints:
            if sortByRef is not None or sortReverse is not None:
                raise SchemaError(
                    "'orderConstraints' cannot be combined with 'sortByRef' or 'sortReverse'",
                    schema)

        head_ids = []

        for i, key_1, item_1 in self.iter_index_key_item(walk, head, idRef):
            for j, key_2, item_2 in self.iter_index_key_item(walk, head, idRef):
                if j < i:
                    if key_1 == key_2:
                        raise HeadInstanceError("Id '%s' was not unique in head" % (key_1,), item_1)
                else:
                    break

        for i, head_key, head_item in self.iter_index_key_item(walk, head, idRef):

            if orderConstraints and head_key != ignoreId:
                head_ids.append(head_key)

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

        if orderConstraints:
            constraints = self.get_order_constraints(walk, schema)
            constraints.add_order(head_ids, self.order_source(walk))

            base_pairs = [(key, item.val)
                          for i, key, item in self.iter_index_key_item(walk, base, idRef)]
            base_ids = [key for key, val in base_pairs]

            for gone_id in [id for id in constraints.ids() if id not in base_ids]:
                constraints.remove(gone_id)

            positions = dict((key, j) for j, key in enumerate(constraints.order(base_ids)))
            ordered_vals = [val for key, val in sorted(
                base_pairs, key=lambda pair: positions[pair[0]])]

            # keep items without a resolvable ID in their original places
            new_val = []
            for item in base:
                try:
                    self.get_key(walk, item, idRef)
                except jsonschema.RefResolutionError:
                    new_val.append(item.val)
                else:
                    new_val.append(ordered_vals.pop(0))
            base.val = new_val

            self.set_order_constraints(walk, schema, constraints)
        else:
            self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_order_constraints(self, walk, schema):
        state = getattr(walk, 'order_state', None)
        if state is None:
            state = {}
            walk.order_state = state

        key = schema.ref
        serialized = state.get(key)
        if serialized is None:
            return OrderConstraints()

        return OrderConstraints.from_state(serialized)

    def set_order_constraints(self, walk, schema, constraints):
        if getattr(walk, 'order_state', None) is None:
            walk.order_state = {}

        walk.order_state[schema.ref] = constraints.to_state()

    def order_source(self, walk):
        index = getattr(walk, 'order_source_index', 0)
        walk.order_source_index = index + 1
        return "merge-%d" % index

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
