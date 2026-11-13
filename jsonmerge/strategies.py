# vim:ts=4 sw=4 expandtab softtabstop=4
from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError
from jsonmerge.jsonvalue import JSONValue
from jsonmerge.order_constraints import OrderConstraints, \
                                       OrderConstraintError, \
                                       canonical_id
import jsonschema
import re
import json

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

    def _looks_like_order_state(self, value):
        if not isinstance(value, dict):
            return False

        return any(key in value for key in (
                '_order_constraints',
                'first_seen',
                'firstSeen',
                'edges',
                'constraints',
                'seen',
                'order',
                'ids'))

    def _normalize_removed_ids(self, value):
        if value is None:
            return []
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, list):
            return value
        return [value]

    def _order_options(self, order_constraints, order_constraints_state,
            order_constraint_source, source, removed, **kwargs):
        enabled = False
        prior = False
        state = None
        target = None
        source_name = order_constraint_source if order_constraint_source is not None else source
        base_source = 'base'
        removed_ids = list(removed)

        if source_name is None:
            source_name = 'head'

        if order_constraints is None and kwargs.get('orderConstraints') is not None:
            order_constraints = kwargs.get('orderConstraints')
        if order_constraints_state is None and kwargs.get('orderConstraintsState') is not None:
            order_constraints_state = kwargs.get('orderConstraintsState')
        if order_constraint_source is None and kwargs.get('orderConstraintSource') is not None:
            order_constraint_source = kwargs.get('orderConstraintSource')

        if order_constraints_state is not None:
            enabled = True
            prior = True
            if isinstance(order_constraints_state, OrderConstraints):
                state = order_constraints_state
            else:
                state = OrderConstraints.from_dict(order_constraints_state)
                if isinstance(order_constraints_state, dict):
                    target = order_constraints_state

        if order_constraints is None or order_constraints is False:
            return {
                'enabled': enabled,
                'prior': prior,
                'state': state,
                'target': target,
                'source': source_name,
                'base_source': base_source,
                'removed': removed_ids,
            }

        if isinstance(order_constraints, str):
            try:
                parsed = json.loads(order_constraints)
            except ValueError:
                parsed = None

            if isinstance(parsed, dict) and self._looks_like_order_state(parsed):
                enabled = True
                prior = True
                if state is None:
                    state = OrderConstraints.from_dict(parsed)
                return {
                    'enabled': enabled,
                    'prior': prior,
                    'state': state,
                    'target': target,
                    'source': source_name,
                    'base_source': base_source,
                    'removed': removed_ids,
                }

        if isinstance(order_constraints, OrderConstraints):
            enabled = True
            prior = True
            if state is None:
                state = order_constraints
            return {
                'enabled': enabled,
                'prior': prior,
                'state': state,
                'target': target,
                'source': source_name,
                'base_source': base_source,
                'removed': removed_ids,
            }

        if isinstance(order_constraints, dict):
            if self._looks_like_order_state(order_constraints):
                enabled = True
                prior = True
                if state is None:
                    state = OrderConstraints.from_dict(order_constraints)
                target = order_constraints
                return {
                    'enabled': enabled,
                    'prior': prior,
                    'state': state,
                    'target': target,
                    'source': source_name,
                    'base_source': base_source,
                    'removed': removed_ids,
                }

            enabled = order_constraints.get('enabled', True)
            nested_state = (
                    order_constraints.get('state')
                    or order_constraints.get('constraints_state')
                    or order_constraints.get('order_constraints_state'))

            if nested_state is not None:
                enabled = True
                prior = True
                if isinstance(nested_state, OrderConstraints):
                    state = nested_state
                else:
                    state = OrderConstraints.from_dict(nested_state)
                    if isinstance(nested_state, dict):
                        target = nested_state

            if order_constraints.get('source') is not None:
                source_name = order_constraints.get('source')
            if order_constraints.get('base_source') is not None:
                base_source = order_constraints.get('base_source')

            for key in ('remove', 'removeId', 'removeIds', 'remove_ids',
                        'deletedIds', 'deleted_ids',
                        'deleteId', 'deleteIds', 'delete_ids',
                        'dropId', 'dropIds', 'drop_ids'):
                if key in order_constraints:
                    removed_ids.extend(self._normalize_removed_ids(
                            order_constraints.get(key)))

            if enabled and state is None:
                state = OrderConstraints()
                if not order_constraints:
                    target = order_constraints

            return {
                'enabled': enabled,
                'prior': prior,
                'state': state,
                'target': target,
                'source': source_name,
                'base_source': base_source,
                'removed': removed_ids,
            }

        enabled = True
        if state is None:
            state = OrderConstraints()
        if isinstance(order_constraints, str) and source_name is None:
            source_name = order_constraints

        return {
            'enabled': enabled,
            'prior': prior,
            'state': state,
            'target': target,
            'source': source_name,
            'base_source': base_source,
            'removed': removed_ids,
        }

    def _validate_unique_ids(self, walk, jv, idRef, error_cls, type_aware):
        seen = {}

        for i, key, item in self.iter_index_key_item(walk, jv, idRef):
            token = canonical_id(key) if type_aware else key
            if token in seen:
                raise error_cls(
                        "Id '%s' was not unique" % (key,), item)
            seen[token] = i

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None,
            sortByRef=None, sortReverse=None, order_constraints=None,
            order_constraints_state=None, order_constraint_source=None,
            source=None, remove=None, removeIds=None, remove_ids=None,
            removeId=None, dropId=None, dropIds=None, drop_ids=None,
            deletedIds=None, deleted_ids=None, deleteIds=None,
            delete_ids=None, deleteId=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        removed = []
        for value in (remove, removeId, removeIds, remove_ids,
                      dropId, dropIds, drop_ids,
                      deletedIds, deleted_ids,
                      deleteId, deleteIds, delete_ids):
            removed.extend(self._normalize_removed_ids(value))

        options = self._order_options(
                order_constraints,
                order_constraints_state,
                order_constraint_source,
                source,
                removed,
                **kwargs)

        if options['enabled'] and (sortByRef is not None or sortReverse is not None):
            raise OrderConstraintError(
                    "'order_constraints' cannot be combined with 'sortByRef' "
                    "or 'sortReverse'",
                    value=schema)

        type_aware = options['enabled']

        if type_aware:
            self._validate_unique_ids(
                    walk, head, idRef, HeadInstanceError, True)
            self._validate_unique_ids(
                    walk, base, idRef, BaseInstanceError, True)
        else:
            for i, key_1, item_1 in self.iter_index_key_item(walk, head, idRef):
                for j, key_2, item_2 in self.iter_index_key_item(walk, head, idRef):
                    if j < i:
                        if key_1 == key_2:
                            raise HeadInstanceError("Id '%s' was not unique in head" % (key_1,), item_1)
                    else:
                        break

        original_base_ids = []
        if type_aware:
            for i, key, item in self.iter_index_key_item(walk, base, idRef):
                original_base_ids.append(key)

        for i, head_key, head_item in self.iter_index_key_item(walk, head, idRef):

            if head_key == ignoreId:
                continue

            matching_j = []
            for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):

                if (canonical_id(base_key) == canonical_id(head_key)
                        if type_aware else base_key == head_key):
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

        if not options['enabled']:
            self.sort_array(walk, base, sortByRef, sortReverse)
            return base

        state = options['state']

        removed_tokens = set(
                canonical_id(item_id)
                for item_id in options['removed'])
        kept_base_ids = [
            key for key in original_base_ids
            if canonical_id(key) not in removed_tokens
        ]

        if options['removed']:
            base.val = [
                item.val for item in base
                if not self._item_has_token(walk, item, idRef, removed_tokens)
            ]

            for item_id in options['removed']:
                state.remove(item_id)

        if not options['prior']:
            state.add_order(kept_base_ids, source=options['base_source'])
        else:
            # A prior state is authoritative.  Do not recreate constraints
            # from the order of an already sorted base array.  Newly seen
            # base members are still registered so that they are not lost.
            for key in kept_base_ids:
                state.add_id(key)

        head_ids = []
        head_token_to_item = {}
        for i, key, item in self.iter_index_key_item(walk, head, idRef):
            if key == ignoreId:
                continue
            token = canonical_id(key)
            if token in head_token_to_item:
                # Duplicate IDs were checked above.  Keeping this guard makes
                # the invariant explicit if a subclass changes get_key().
                raise HeadInstanceError(
                        "Id '%s' was not unique in head" % (key,), item)
            head_token_to_item[token] = item
            head_ids.append(key)

        state.add_order(head_ids, source=options['source'])

        ordered_ids = state.order()

        item_map = {}
        idless = []
        remaining = []
        for i, item in enumerate(base):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                idless.append(item.val)
                continue

            token = canonical_id(key)
            if token in item_map:
                raise BaseInstanceError(
                        "Id '%s' was not unique in base" % (key,), item)
            item_map[token] = item.val
            remaining.append(token)

        ordered_tokens = set()
        ordered_items = list(idless)
        for item_id in ordered_ids:
            token = canonical_id(item_id)
            if token in item_map:
                ordered_items.append(item_map[token])
                ordered_tokens.add(token)

        # Keep any member that was not represented in the state (for
        # example a member with an ID type that the caller changed between
        # calls).  Its position is its first occurrence in the current base.
        for token in remaining:
            if token not in ordered_tokens:
                ordered_items.append(item_map[token])

        base.val = ordered_items

        if options['target'] is not None:
            serialized = state.to_dict()
            options['target'].clear()
            options['target'].update(serialized)

        walk.order_constraint_states[base.ref] = state
        walk.order_constraints_enabled = True

        return base

    def _item_has_token(self, walk, item, idRef, tokens):
        try:
            key = self.get_key(walk, item, idRef)
        except jsonschema.RefResolutionError:
            return False
        return canonical_id(key) in tokens

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
