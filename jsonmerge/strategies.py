# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.jsonvalue import JSONValue
import contextlib
import copy
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

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None, sortReverse=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

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


class Discriminator(Strategy):
    """A Strategy that selects a registered subschema by a discriminator field.

    This is useful when items of an array can be one of several types (e.g.
    *text*, *image* or *link*), each with its own merge strategies. The
    branch to use is chosen explicitly by the value of a discriminator
    field in the merged object, not by trying which subschema of *anyOf*
    or *oneOf* happens to validate.

    The *field* option names the object property used as the discriminator
    (default *type*). The *mapping* option must map each possible
    discriminator value to a schema. If *base* and *head* select the same
    branch, they are merged recursively using that branch's schema. If they
    select different branches, the *onTypeChange* option controls the
    result: *replace* (the default) replaces the whole item with *head*
    merged as if *base* was undefined, so that no state from the old branch
    leaks into the new one. *reject* raises an error instead.

    An object without the discriminator field, or with a value that has no
    registered schema, raises an error pointing to the object's location in
    the document. The same is true for unresolvable schema references in
    the selected branch.
    """

    STORE_URI = 'urn:jsonmerge:discriminator'

    def _check_options(self, field, mapping, onTypeChange, schema):
        if not isinstance(field, str):
            raise SchemaError("Discriminator 'field' option must be a string", schema)

        if not isinstance(mapping, dict) or not mapping:
            raise SchemaError("Discriminator strategy requires a non-empty "
                    "'mapping' option", schema)

        for name, subschema in mapping.items():
            if not isinstance(subschema, dict):
                raise SchemaError("Schema registered for discriminator value "
                        "'%s' is not an object" % (name,), schema)

        if onTypeChange not in ('replace', 'reject'):
            raise SchemaError("Unknown onTypeChange mode '%s'" % (onTypeChange,), schema)

    def _branch_name(self, item, field, mapping, exc_class):
        if field not in item.val:
            raise exc_class("Object does not contain the discriminator "
                    "field '%s'" % (field,), item)

        name = item.val[field]

        try:
            known = name in mapping
        except TypeError:
            known = False

        if not known:
            raise exc_class("No schema registered for discriminator value "
                    "'%s'" % (name,), item)

        return name

    def _escape_pointer(self, key):
        return str(key).replace('~', '~0').replace('/', '~1')

    def _mapping_node(self, schema):
        node = schema.get('mergeOptions')
        if node.is_undef():
            return node
        return node.get('mapping')

    @contextlib.contextmanager
    def _registered_mapping(self, walk, container):
        # Temporarily register a mapping container with the resolver under
        # a synthetic URI. Used when the mapping is not reachable from the
        # schema document (e.g. it was given through merger-level
        # merge_options).
        store = walk.resolver.store
        sentinel = object()
        old = store.get(self.STORE_URI, sentinel)
        store[self.STORE_URI] = container
        try:
            yield
        finally:
            if old is sentinel:
                del store[self.STORE_URI]
            else:
                store[self.STORE_URI] = old

    @contextlib.contextmanager
    def _branch_schema(self, walk, schema, mapping, name):
        node = self._mapping_node(schema)
        if not node.is_undef():
            subschema = node.get(name)
            if not subschema.is_undef() and subschema.val is mapping[name]:
                # The mapping is a part of the schema document. Navigate to
                # the branch so that references within it resolve against
                # the original document.
                yield subschema
                return

        with self._registered_mapping(walk, {'mapping': mapping}):
            ref = self.STORE_URI + '#/mapping/' + self._escape_pointer(name)
            yield JSONValue(mapping[name], ref)

    @contextlib.contextmanager
    def _schema_branch(self, walk, schema, mapping, name):
        # Descend into a copy of the registered branch schema so that the
        # shared schema object registered in the mapping is never modified.
        branch = copy.deepcopy(mapping[name])

        try:
            url, resolved = walk.resolver.resolve(schema.ref)
        except jsonschema.RefResolutionError:
            resolved = None

        doc_mapping = None
        if isinstance(resolved, dict):
            doc_mapping = resolved.get('mergeOptions', {}).get('mapping')

        if isinstance(doc_mapping, dict) and doc_mapping.get(name) is mapping[name]:
            # The mapping is a part of the schema document. Temporarily
            # swap in the copy so that the walk can resolve it (and any
            # local references it contains) against the original document.
            original = doc_mapping[name]
            doc_mapping[name] = branch
            try:
                ref = schema.ref + '/mergeOptions/mapping/' + self._escape_pointer(name)
                yield JSONValue(branch, ref)
            finally:
                doc_mapping[name] = original
            return

        with self._registered_mapping(walk, {'mapping': {name: branch}}):
            ref = self.STORE_URI + '#/mapping/' + self._escape_pointer(name)
            yield JSONValue(branch, ref)

    def merge(self, walk, base, head, schema, field='type', mapping=None,
            onTypeChange='replace', **kwargs):
        self._check_options(field, mapping, onTypeChange, schema)

        if not walk.is_type(head, 'object'):
            raise HeadInstanceError('Head is not an object', head)

        head_branch = self._branch_name(head, field, mapping, HeadInstanceError)

        if not base.is_undef():
            if not walk.is_type(base, 'object'):
                raise BaseInstanceError('Base is not an object', base)

            base_branch = self._branch_name(base, field, mapping, BaseInstanceError)

            if base_branch != head_branch:
                if onTypeChange == 'reject':
                    raise HeadInstanceError(
                            "Discriminator value changed from '%s' to '%s'" %
                            (base_branch, head_branch), head)

                # Replace the whole item: merge head as if base was
                # undefined, so that no state from the old branch (e.g.
                # version history of a text item) leaks into the new one.
                base = JSONValue(undef=True)

        with self._branch_schema(walk, schema, mapping, head_branch) as subschema:
            try:
                return walk.descend(subschema, base, head)
            except jsonschema.RefResolutionError as exc:
                raise SchemaError("Unresolvable schema reference in schema "
                        "registered for discriminator value '%s': %s" %
                        (head_branch, exc), head)

    def get_schema(self, walk, schema, field='type', mapping=None,
            onTypeChange='replace', **kwargs):
        self._check_options(field, mapping, onTypeChange, schema)

        branches = []
        for name in mapping:
            with self._schema_branch(walk, schema, mapping, name) as subschema:
                branches.append(walk.descend(subschema).val)

        rv = dict(schema.val)
        rv['oneOf'] = branches

        return JSONValue(rv, schema.ref)

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
