# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.jsonvalue import JSONValue
from jsonmerge import deletion
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

        delete_opts = deletion.resolve(schema, **kwargs)

        escaped = False
        if delete_opts.enable:
            # Malformed markers must abort before any history is appended.
            deletion.prevalidate(head)

            # An escaped head is ordinary data, even if its payload looks
            # like a delete marker; record it verbatim as a version value.
            if deletion.is_escape_object(head.val):
                head = head._subval(deletion.ESCAPE_KEY,
                                    val=deletion.unescape(head.val))
                escaped = True

        is_delete = (delete_opts.enable and not escaped
                     and deletion.is_delete_marker(head.val))

        if base.is_undef():
            base = JSONValue(val=[], ref=base.ref)
            last_entry = JSONValue(undef=True)
            last_is_tombstone = False
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

                last_is_tombstone = bool(
                    last_entry.val.get(deletion.DELETED_KEY, False))

                if 'value' not in last_entry.val and not last_is_tombstone:
                    raise BaseInstanceError("Last entry in the versioned array has no 'value' property. "
                            "Base not previously generated with this strategy?", last_entry)
            else:
                last_entry = JSONValue(undef=True)
                last_is_tombstone = False

        if is_delete:
            # Deletions are part of the history: append a tombstone entry so
            # the versioned array records when the value ceased to exist.
            changed = last_entry.is_undef() or not last_is_tombstone

            if not ignoreDups or changed:
                entry = dict(metadata) if metadata is not None else dict()
                entry[deletion.DELETED_KEY] = True
                base.val.append(entry)
                if limit is not None:
                    base.val = base.val[-limit:]
        else:
            # A value that reappears after a tombstone is always a change.
            changed = (last_entry.is_undef() or last_is_tombstone
                       or last_entry['value'].val != head.val)

            if not ignoreDups or changed:
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

        # A tombstone entry (see Version.merge) carries this boolean instead
        # of a 'value' property.  It is only produced when the deletion
        # protocol is enabled, so declare it only then to keep the schema in
        # sync with documents the strategy can actually output.
        if kwargs.get('enableDelete'):
            item['properties'][deletion.DELETED_KEY] = {"type": "boolean"}

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

    def head_key(self, walk, index, item, idRef):
        """Key used for a head member. Overridden by index-based merging."""
        return self.get_key(walk, item, idRef)

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

        delete_opts = deletion.resolve(schema, **kwargs)

        if delete_opts.enable:
            # Validate the whole head (including marker shapes and nested
            # markers) before any member is removed or appended.
            deletion.prevalidate(head)

        # Pre-process escaped members: build a per-index map of effective
        # (unescaped) items and remember which indices were escaped. The
        # payload is reachable at the deeper escape pointer, which keeps the
        # resolver identity invariant satisfied downstream. Escaped members
        # are ordinary data afterwards and must never be treated as markers.
        effective = {}
        escaped_indices = set()
        for index, head_item in enumerate(head):
            if not delete_opts.enable:
                continue

            if deletion.is_escape_object(head_item.val):
                effective[index] = head_item._subval(
                    deletion.ESCAPE_KEY,
                    val=deletion.unescape(head_item.val))
                escaped_indices.add(index)
            elif deletion.is_array_delete_marker(head_item.val):
                # A member delete marker must identify its target.
                try:
                    self.head_key(walk, index, head_item, idRef)
                except jsonschema.RefResolutionError:
                    raise HeadInstanceError(
                        "Member delete marker does not contain the id "
                        "referenced by idRef '%s'" % idRef, head_item)

        def iter_head():
            # Resolve each key against the effective (possibly unescaped)
            # item, while yielding an item whose resolver pointer stays
            # valid downstream.
            for index, original_item in enumerate(head):
                head_item = effective.get(index, original_item)
                try:
                    key = self.head_key(walk, index, head_item, idRef)
                except jsonschema.RefResolutionError:
                    continue
                yield key, head_item, index in escaped_indices

        for i, (key_1, item_1, _esc_1) in enumerate(iter_head()):
            for j, (key_2, item_2, _esc_2) in enumerate(iter_head()):
                if j < i:
                    if key_1 == key_2:
                        raise HeadInstanceError("Id '%s' was not unique in head" % (key_1,), item_1)
                else:
                    break

        for head_key, head_item, escaped in iter_head():

            if head_key == ignoreId:
                continue

            if (delete_opts.enable and not escaped
                    and deletion.is_array_delete_marker(head_item.val)):
                # An array member delete marker carries only the item id and
                # the marker property, e.g. {"id": "A", "__delete__": null}.
                matching_j = [j for j, base_key, _ in
                              self.iter_index_key_item(walk, base, idRef)
                              if base_key == head_key]

                if len(matching_j) == 1:
                    del base.val[matching_j[0]]
                elif len(matching_j) == 0:
                    if delete_opts.strict:
                        raise BaseInstanceError(
                            "Delete marker for id '%s' that is not present "
                            "in base" % (head_key,), head_item)
                else:
                    j = matching_j[1]
                    raise BaseInstanceError(
                        "Id '%s' was not unique in base" % (head_key,),
                        base[j])
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

    def head_key(self, walk, index, item, idRef):
        return index

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

    The explicit deletion protocol is enabled with the enableDelete option.
    When enabled, a head property whose value is the delete marker removes
    that property from the result.  The strictDelete option makes deleting
    a property that is absent from base an error instead of a no-op.
    """
    def merge(self, walk, base, head, schema, objclass_menu=None, objClass='_default', **kwargs):
        if not walk.is_type(head, "object"):
            raise HeadInstanceError("Head is not an object", head)

        delete_opts = deletion.resolve(schema, **kwargs)

        if delete_opts.enable:
            # Reject malformed markers before any property is touched, so a
            # bad head can never delete part of the data and then fail.
            deletion.prevalidate(head)

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

            if delete_opts.enable:
                if deletion.is_delete_marker(v.val):
                    if k in base.val:
                        del base.val[k]
                    elif delete_opts.strict:
                        raise BaseInstanceError(
                            "Delete marker for property '%s' that is not "
                            "present in base" % k, base)
                    continue

                if deletion.is_escape_object(v.val):
                    # Point at the escaped payload, which is itself reachable
                    # in the head at the deeper .../__escaped__ pointer, so
                    # the resolver identity invariant stays satisfied.
                    v = v._subval(deletion.ESCAPE_KEY,
                                  val=deletion.unescape(v.val))

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
