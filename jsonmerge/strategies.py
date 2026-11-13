# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.jsonvalue import JSONValue
from jsonmerge import markers
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
        if markers.is_enabled(kwargs):
            markers.reject_unsupported(walk, schema, kwargs)
        return head

    def get_schema(self, walk, schema, **kwargs):
        if markers.is_enabled(kwargs):
            markers.reject_unsupported(walk, schema, kwargs)
        return schema

class Discard(Strategy):
    def merge(self, walk, base, head, schema, keepIfUndef=False, **kwargs):
        if markers.is_enabled(kwargs):
            markers.reject_unsupported(walk, schema, kwargs)
        if base.is_undef() and keepIfUndef:
            return head
        else:
            return base

    def get_schema(self, walk, schema, **kwargs):
        if markers.is_enabled(kwargs):
            markers.reject_unsupported(walk, schema, kwargs)
        return schema

class Version(Strategy):

    def add_metadata(self, head, metadata):
        if metadata is None:
            rv = dict()
        else:
            rv = dict(metadata)

        rv['value'] = head
        return rv

    def add_deleted_metadata(self, metadata):
        if metadata is None:
            rv = dict()
        else:
            rv = dict(metadata)

        rv['deleted'] = True
        return rv

    def merge(self, walk, base, head, schema, limit=None, unique=None, ignoreDups=True, metadata=None, **kwargs):

        # backwards compatibility
        if unique is False:
            ignoreDups = False

        if metadata is not None:
            if not walk.is_type(JSONValue(val=metadata), "object"):
                raise SchemaError("'metadata' option does not contain an object")

        delete_enabled = markers.is_enabled(kwargs)
        if delete_enabled:
            markers.validate_options(kwargs, schema)
            kind, head = markers.classify_object(head, kwargs)
            if kind == "delete":
                deleted = True
            else:
                deleted = False
                # "escape" yields the unwrapped user data; "value" leaves
                # head unchanged. In both cases the marker is consumed.
        else:
            deleted = False

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

                if 'value' not in last_entry.val and 'deleted' not in last_entry.val:
                    raise BaseInstanceError("Last entry in the versioned array has no 'value' property. "
                            "Base not previously generated with this strategy?", last_entry)
            else:
                last_entry = JSONValue(undef=True)

        def entry_matches(entry):
            if deleted:
                return entry.val.get('deleted') is True
            else:
                return ('value' in entry.val and
                        entry['value'].val == head.val)

        if not ignoreDups or last_entry.is_undef() or not entry_matches(last_entry):
            if deleted:
                base.val.append(self.add_deleted_metadata(metadata))
            else:
                base.val.append(self.add_metadata(head.val, metadata))
            if limit is not None:
                base.val = base.val[-limit:]

        return base

    def get_schema(self, walk, schema, limit=None, metadataSchema=None, **kwargs):
        delete_enabled = markers.is_enabled(kwargs)
        if delete_enabled:
            markers.validate_options(kwargs, schema)

        if metadataSchema is not None:
            item = dict(walk.resolve_subschema_option_refs(metadataSchema))
        else:
            item = {}

        if 'properties' not in item:
            item['properties'] = {}
        else:
            item['properties'] = dict(item['properties'])

        item['properties']['value'] = schema.val

        if delete_enabled:
            item['properties']['deleted'] = {"type": "boolean"}

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
        if markers.is_enabled(kwargs):
            markers.reject_unsupported(walk, schema, kwargs)
        base.val += head.val

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        schema.val.pop('maxItems', None)
        schema.val.pop('uniqueItems', None)

        return schema

class ArrayMergeById(ArrayStrategy):

    supports_delete = True

    def get_key(self, walk, item, idRef):
        return self._resolve_ref(walk, item, idRef)

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            yield i, key, item

    def _resolve_item_key(self, walk, item, idRef):
        """Resolve the item ID. Returns None if the ID cannot be resolved."""
        try:
            return self.get_key(walk, item, idRef)
        except jsonschema.RefResolutionError:
            return None

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None, sortReverse=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        delete_enabled = markers.is_enabled(kwargs)
        if delete_enabled:
            markers.validate_options(kwargs, schema)
            if not self.supports_delete:
                markers.reject_unsupported(walk, schema, kwargs)

        if not delete_enabled:
            return self._merge_plain(walk, base, head, subschema, idRef,
                    ignoreId, sortByRef, sortReverse)

        # Phase 1: classify every head item and validate the whole merge
        # before base is mutated in any way.
        entries = []
        seen_keys = []

        for head_item in head:
            kind, payload = markers.classify_array_item(head_item, kwargs)

            if kind == "delete":
                markers.validate_array_delete_item(payload, kwargs, idRef)
                key = self._resolve_item_key(walk, payload, idRef)
                if key is None:
                    raise markers.MarkerError(
                        "Delete marker has no resolvable ID (idRef %r)"
                        % (idRef,), payload)
                effective = payload
            elif kind == "escape":
                effective = payload
                key = self._resolve_item_key(walk, effective, idRef)
            else:
                effective = head_item
                key = self._resolve_item_key(walk, effective, idRef)

            if key is not None:
                for seen_key, seen_item in seen_keys:
                    if seen_key == key:
                        raise HeadInstanceError(
                            "Id '%s' was not unique in head" % (key,), effective)
                seen_keys.append((key, effective))

            entries.append((kind, key, effective))

        base_keys = self._reindex_base(walk, base, idRef)

        if kwargs.get(markers.STRICT_OPTION, False):
            for kind, key, effective in entries:
                if kind == "delete" and not self._find_matches(base_keys, key):
                    raise markers.MarkerError(
                        "Delete marker targets ID '%s', which is not present "
                        "in base" % (key,), effective)

        # Phase 2: all markers validated - apply the merge.
        for kind, head_key, head_item in entries:

            if head_key is not None and head_key == ignoreId:
                continue

            if head_key is not None:
                matching_j = self._find_matches(base_keys, head_key)
            else:
                matching_j = []

            if kind == "delete":
                if len(matching_j) == 1:
                    del base.val[matching_j[0]]
                    base_keys = self._reindex_base(walk, base, idRef)
                elif len(matching_j) > 1:
                    raise BaseInstanceError(
                        "Id '%s' was not unique in base" % (head_key,),
                        base[matching_j[1]])
                # zero matches: idempotent no-op (strict mode already failed)
                continue

            if kind == "escape":
                # Escaped user data is stored verbatim and is never
                # interpreted as a (possibly nested) marker.
                if len(matching_j) == 1:
                    base.val[matching_j[0]] = head_item.val
                elif len(matching_j) == 0:
                    base.val.append(head_item.val)
                else:
                    raise BaseInstanceError(
                        "Id '%s' was not unique in base" % (head_key,),
                        base[matching_j[1]])
                base_keys = self._reindex_base(walk, base, idRef)
                continue

            if len(matching_j) == 1:
                # If there was exactly one match, we replace it with a merged item
                j = matching_j[0]
                base[j] = walk.descend(subschema, base[j], head_item)
            elif len(matching_j) == 0:
                # If there wasn't a match, we append a new object
                base.append(walk.descend(subschema, JSONValue(undef=True), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError("Id '%s' was not unique in base" % (head_key,), base[j])

            base_keys = self._reindex_base(walk, base, idRef)

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def _find_matches(self, base_keys, key):
        for base_key, indexes in base_keys:
            if base_key == key:
                return indexes
        return []

    def _reindex_base(self, walk, base, idRef):
        base_keys = []
        for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):
            if base_keys and base_keys[-1][0] == base_key:
                base_keys[-1][1].append(j)
            else:
                found = False
                for entry in base_keys:
                    if entry[0] == base_key:
                        entry[1].append(j)
                        found = True
                        break
                if not found:
                    base_keys.append([base_key, [j]])
        return base_keys

    def _merge_plain(self, walk, base, head, subschema, idRef, ignoreId,
            sortByRef, sortReverse):
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

    supports_delete = False

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

        delete_enabled = markers.is_enabled(kwargs)
        if delete_enabled:
            markers.validate_options(kwargs, schema)

        # Phase 1: validate every marker in head before mutating base, so a
        # malformed marker never results in a partially applied merge.
        if delete_enabled:
            for key, value in head.items():
                markers.classify_object(value, kwargs)

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

            if delete_enabled:
                kind, payload = markers.classify_object(v, kwargs)

                if kind == "delete":
                    if k in base.val:
                        del base.val[k]
                    elif kwargs.get(markers.STRICT_OPTION, False):
                        raise markers.MarkerError(
                            "Delete marker targets property '%s', which is "
                            "not present in base" % (k,), v)
                    continue

                if kind == "escape":
                    # User data that literally looks like a marker is stored
                    # verbatim. The escape wrapper never reaches output.
                    base[k] = payload
                    continue

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
