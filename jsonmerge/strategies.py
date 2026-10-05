# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
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

    BRANCH_OPTIONS = (
            'branches',
            'mapping',
    )

    TYPE_CHANGE_OPTIONS = (
            'onTypeChange',
    )

    DISCRIMINATOR_OPTIONS = (
            'discriminatorRef',
    )

    def get_key(self, walk, item, idRef):
        return self._resolve_ref(walk, item, idRef)

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            yield i, key, item

    def _merge_option(self, schema, kwargs, *names):
        options = schema.get('mergeOptions')

        if not options.is_undef():
            for name in names:
                if name in options.val:
                    return options.get(name)

        for name in names:
            if name in kwargs:
                return JSONValue(kwargs[name],
                        schema.ref + '/mergeOptions/' + name)

        return JSONValue(undef=True)

    def _resolve_schema(self, walk, schema):
        if schema.is_undef() or not walk.is_type(schema, 'object'):
            return schema

        ref = schema.val.get('$ref')
        if ref is None:
            return schema

        try:
            with walk.resolver.resolving(ref) as resolved:
                if not isinstance(resolved, dict):
                    raise SchemaError("'$ref' does not point to an object",
                            schema)
                return JSONValue(resolved, ref)
        except jsonschema.RefResolutionError:
            return JSONValue(undef=True)

    def _discriminator_pointer(self, ref):
        if ref.startswith('/'):
            return ref
        else:
            return '/' + ref

    def _mapping_branches(self, walk, mapping):
        branches = []
        for value, branch in mapping.items():
            if not walk.is_type(branch, 'string') and \
                    not walk.is_type(branch, 'object'):
                raise SchemaError("Discriminator branch is not a schema",
                        branch)
            branches.append((value, branch))
        return branches

    def _discriminator_config(self, walk, schema, subschema, kwargs):
        discriminator_ref = self._merge_option(schema, kwargs,
                *self.DISCRIMINATOR_OPTIONS)
        branch_option = self._merge_option(schema, kwargs,
                *self.BRANCH_OPTIONS)
        type_change = self._merge_option(schema, kwargs,
                *self.TYPE_CHANGE_OPTIONS)

        if type_change.is_undef():
            type_change = JSONValue('replace', schema.ref)
        elif not walk.is_type(type_change, 'string') or \
                type_change.val not in ('replace', 'reject'):
            raise SchemaError(
                    "Type change behavior must be 'replace' or 'reject'",
                    type_change)

        item_schema = self._resolve_schema(walk, subschema)

        mapping = branch_option
        item_discriminator = JSONValue(undef=True)
        option_discriminator = self._merge_option(schema, kwargs,
                'discriminator')

        if not item_schema.is_undef() and walk.is_type(item_schema, 'object'):
            item_discriminator = item_schema.get('discriminator')

        if not item_discriminator.is_undef():
            if not walk.is_type(item_discriminator, 'object'):
                raise SchemaError("Discriminator is not an object",
                        item_discriminator)

            discriminator_ref = item_discriminator.get('propertyName')
            mapping = item_discriminator.get('mapping')
        elif walk.is_type(option_discriminator, 'object'):
            discriminator_ref = option_discriminator.get('propertyName')
            mapping = option_discriminator.get('mapping')
        elif walk.is_type(option_discriminator, 'string'):
            discriminator_ref = option_discriminator

        if discriminator_ref.is_undef():
            return None

        if not walk.is_type(discriminator_ref, 'string') or \
                not discriminator_ref.val:
            raise SchemaError("Discriminator property name is not a string",
                    discriminator_ref)

        branches = None

        if mapping.is_undef() and not item_schema.is_undef() and \
                walk.is_type(item_schema, 'object'):
            one_of = item_schema.get('oneOf')
            if not one_of.is_undef():
                if not walk.is_type(one_of, 'array'):
                    raise SchemaError("'oneOf' is not an array", one_of)

                mapping = JSONValue({}, item_schema.ref + '/oneOf')
                branches = []
                for branch in one_of:
                    try:
                        property_schema = branch.val['properties'][
                                discriminator_ref.val]
                        enum = property_schema['enum']
                        value = enum[0]
                    except (KeyError, IndexError, TypeError):
                        raise SchemaError(
                                "Branch schema is not registered with an "
                                "enum discriminator value", branch)
                    mapping.val[value] = branch.val
                    branches.append((value, branch))

        if mapping.is_undef():
            raise SchemaError("No discriminator branch schemas are registered",
                    item_schema)

        if not walk.is_type(mapping, 'object'):
            raise SchemaError("Discriminator branch mapping is not an object",
                    mapping)

        if branches is None:
            branches = self._mapping_branches(walk, mapping)

        return {
                'branch_order': [value for value, branch in branches],
                'ref': discriminator_ref.val,
                'branches': dict((value, branch)
                        for value, branch in branches),
                'onTypeChange': type_change.val,
        }

    def _discriminator_value(self, walk, item, config, head):
        error_class = HeadInstanceError if head else BaseInstanceError

        try:
            value = self._resolve_ref(walk, item,
                    self._discriminator_pointer(config['ref']))
        except (jsonschema.RefResolutionError, KeyError, TypeError,
                IndexError, AttributeError):
            value = None

        if value is None:
            raise error_class(
                    "Discriminator field '%s' has no value" %
                    (config['ref'],), item)

        return value

    def _resolve_branch(self, walk, value, raw_branch, item):
        if walk.is_type(raw_branch, 'string'):
            ref = raw_branch.val
            branch = JSONValue({'$ref': ref}, raw_branch.ref)
        else:
            branch = raw_branch
            ref = branch.val.get('$ref')

        if ref is not None:
            try:
                with walk.resolver.resolving(ref) as resolved:
                    if not isinstance(resolved, dict):
                        raise SchemaError(
                                "Discriminator branch '%s' does not point "
                                "to an object schema" % (value,), item)
                    branch = JSONValue(resolved, ref)
            except jsonschema.RefResolutionError:
                raise SchemaError(
                        "Unable to resolve schema for discriminator branch "
                        "'%s'" % (value,), item)

        return branch

    def _select_branch(self, walk, item, config, head):
        value = self._discriminator_value(walk, item, config, head)
        raw_branch = config['branches'].get(value)

        if raw_branch is None:
            error_class = HeadInstanceError if head else BaseInstanceError
            raise error_class("Unknown discriminator value '%s'" % (value,),
                    item)

        return value, self._resolve_branch(walk, value, raw_branch, item)

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None, sortReverse=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        discriminator = self._discriminator_config(walk, schema, subschema,
                kwargs)

        head_branches = {}
        if discriminator is not None:
            for i, head_key, head_item in self.iter_index_key_item(
                    walk, head, idRef):
                if head_key == ignoreId:
                    continue
                head_branches[i] = self._select_branch(walk, head_item,
                        discriminator, True)

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

            if discriminator is not None:
                head_value, head_branch = head_branches[i]
            else:
                head_value = None
                head_branch = subschema

            matching_j = []
            for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):

                if base_key == head_key:
                    matching_j.append(j)
                    matched_item = base_item

            if len(matching_j) == 1:
                # If there was exactly one match, we replace it with a merged item
                j = matching_j[0]
                if discriminator is None:
                    merged_item = walk.descend(subschema, matched_item,
                            head_item)
                else:
                    base_value, base_branch = self._select_branch(walk,
                            matched_item, discriminator, False)

                    if base_value == head_value:
                        merged_item = walk.descend(head_branch, matched_item,
                                head_item)
                    elif discriminator['onTypeChange'] == 'replace':
                        merged_item = head_item
                    else:
                        raise HeadInstanceError(
                                "Base and head discriminator values differ: "
                                "'%s' != '%s'" %
                                (base_value, head_value), head_item)

                base[j] = merged_item
            elif len(matching_j) == 0:
                # If there wasn't a match, we append a new object
                base.append(walk.descend(head_branch, JSONValue(
                        undef=True, ref=head_item.ref), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError("Id '%s' was not unique in base" % (base_key,), base[j])

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        subschema = schema.get('items')
        discriminator = self._discriminator_config(walk, schema,
                subschema, kwargs)

        if discriminator is None:
            if not subschema.is_undef():
                schema['items'] = walk.descend(subschema)
        else:
            branches = []
            resolved_refs = set()
            for value in discriminator['branch_order']:
                raw_branch = discriminator['branches'][value]
                branch_ref = None
                if walk.is_type(raw_branch, 'string'):
                    ref = raw_branch.val
                    branch_schema = JSONValue({'$ref': ref},
                            raw_branch.ref)
                else:
                    branch_schema = raw_branch
                    if walk.is_type(branch_schema, 'object'):
                        ref = branch_schema.val.get('$ref')
                    else:
                        ref = None

                if ref is not None and not walk.resolver.is_remote_ref(ref):
                    branch_ref = ref
                    if ref not in resolved_refs:
                        try:
                            with walk.resolver.resolving(ref) as resolved:
                                walk.descend(JSONValue(resolved, ref))
                        except jsonschema.RefResolutionError:
                            raise SchemaError(
                                    "Unable to resolve schema for "
                                    "discriminator branch '%s'" %
                                    (value,), schema.get('items'))
                        resolved_refs.add(ref)

                if branch_ref is not None:
                    branches.append({'$ref': branch_ref})
                elif walk.is_type(branch_schema, 'string'):
                    branches.append(walk.descend(branch_schema).val)
                else:
                    branches.append(walk.descend(branch_schema).val)

            if walk.is_type(subschema, 'object'):
                item_schema = JSONValue(dict(subschema.val),
                        subschema.ref)
                item_schema.val.pop('discriminator', None)
            else:
                item_schema = JSONValue({}, schema.ref + '/items')

            item_schema.val['oneOf'] = branches
            schema['items'] = item_schema

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
