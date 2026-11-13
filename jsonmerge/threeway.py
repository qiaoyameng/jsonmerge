# vim:ts=4 sw=4 expandtab softtabstop=4
"""Independent three-way merge entry point.

Unlike the two-way :func:`jsonmerge.merge`, this module merges two documents
that both diverged from a common *ancestor* document. Divergent edits are
detected instead of silently overwriting one side with the other.
"""

import copy
import re
from collections import OrderedDict

from jsonschema.validators import Draft4Validator

from jsonmerge import strategies as builtin_strategies
from jsonmerge.resolver import LocalRefResolver
from jsonmerge.threeway_exceptions import (
    ThreeWayConflictResolutionError,
    ThreeWayInputError,
    ThreeWayStrategyError,
)


class _Missing(object):
    """Sentinel for a value that is absent at a given path."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(_Missing, cls).__new__(cls)
        return cls._instance

    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False

    # Python 2 compatibility
    __nonzero__ = __bool__


def _public_value(value):
    """Convert internal sentinels into JSON-serializable conflict values."""
    if value is MISSING:
        return None
    if isinstance(value, dict):
        return OrderedDict(
            (key, _public_value(item)) for key, item in value.items())
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


# Public sentinel used in conflict records and accepted in resolutions.
MISSING = _Missing()


def _escape_pointer_token(token):
    return token.replace('~', '~0').replace('/', '~1')


def path_to_pointer(path):
    """Convert an internal tuple path to a JSON pointer string."""
    if not path:
        return ''
    return ''.join('/' + _escape_pointer_token(str(part)) for part in path)


def _is_undef(value):
    return isinstance(value, _Missing)


class Conflict(object):
    """A structured three-way merge conflict.

    Attributes:
        conflict_id: Stable identifier (string) used when supplying a
            resolution.
        path: JSON pointer of the conflicting value relative to the document
            root.
        conflict_type: One of "different_modification", "delete_modify" or
            "duplicate_id".
        ancestor, left, right: The three values. :data:`MISSING` marks a
            value that does not exist in the respective document.
    """

    def __init__(self, conflict_id, path, conflict_type, ancestor, left, right,
                 message=None):
        self.conflict_id = conflict_id
        self.path = path
        self.conflict_type = conflict_type
        self.ancestor = ancestor
        self.left = left
        self.right = right
        self.message = message

    def as_dict(self):
        rv = OrderedDict()
        rv['id'] = self.conflict_id
        rv['path'] = self.path
        rv['type'] = self.conflict_type
        # The "present" flags distinguish an absent path from an explicit JSON
        # null, since both serialize to None. Programmatic callers can also
        # use the Conflict object's attributes, which retain the MISSING
        # sentinel.
        rv['ancestor'] = _public_value(self.ancestor)
        rv['left'] = _public_value(self.left)
        rv['right'] = _public_value(self.right)
        rv['ancestor_present'] = not _is_undef(self.ancestor)
        rv['left_present'] = not _is_undef(self.left)
        rv['right_present'] = not _is_undef(self.right)
        if self.message is not None:
            rv['message'] = self.message
        return rv

    def __repr__(self):
        return 'Conflict(%r, %r, %r)' % (
            self.conflict_id, self.path, self.conflict_type)


class ThreeWayMergeResult(object):
    """Result of a three-way merge.

    ``merged`` is the merged document. Paths with unresolved conflicts are
    omitted from ``merged`` and listed in ``conflicts`` with all three values.
    """

    def __init__(self, merged, conflicts):
        self.merged = merged
        self.conflicts = conflicts

    @property
    def has_conflicts(self):
        return bool(self.conflicts)

    def conflicts_as_dict(self):
        return [conflict.as_dict() for conflict in self.conflicts]

    def as_dict(self):
        return OrderedDict((
            ('merged', self.merged),
            ('conflicts', self.conflicts_as_dict()),
        ))


class _ThreeWayWalk(object):

    def __init__(self, merger):
        self.merger = merger
        self.resolver = merger.resolver
        self.conflicts = []
        self._conflict_seq = 0

    def _next_conflict_id(self):
        conflict_id = 'conflict-%d' % self._conflict_seq
        self._conflict_seq += 1
        return conflict_id

    def report_conflict(self, path, conflict_type, ancestor, left, right,
                        message=None):
        conflict = Conflict(
            self._next_conflict_id(), path_to_pointer(path), conflict_type,
            ancestor, left, right, message)
        self.conflicts.append(conflict)
        return conflict

    def register_conflict(self, path, conflict_type, ancestor, left, right,
                          message=None):
        """Record a conflict and apply any caller-supplied resolution.

        Returns a tuple ``(conflict, resolved)``. When no resolution applies,
        ``conflict`` is the recorded :class:`Conflict` and ``resolved`` is
        :data:`MISSING`. When a resolution applies, no conflict is recorded
        and ``resolved`` holds the caller's value (possibly :data:`MISSING`
        for a deletion).
        """
        conflict = self.report_conflict(
            path, conflict_type, ancestor, left, right, message)
        return conflict, MISSING

    def resolve_schema(self, schema):
        """Follow $ref keywords, guarding against recursive references."""
        seen = set()
        current = schema
        while isinstance(current, dict) and '$ref' in current:
            ref = current['$ref']
            if ref in seen:
                break
            seen.add(ref)
            with self.resolver.resolving(ref) as resolved:
                current = resolved
        return current

    def strategy_for(self, schema):
        """Return (strategy_name, strategy, merge_options) for a schema."""
        schema = self.resolve_schema(schema)

        if not isinstance(schema, dict):
            return None, None, {}

        name = schema.get('mergeStrategy')

        if name is None:
            return None, None, {}

        try:
            strategy = self.merger.strategies[name]
        except KeyError:
            raise ThreeWayStrategyError(
                "Unknown strategy '%s' in merge schema" % name)

        if not getattr(strategy, 'three_way_capable', False):
            raise ThreeWayStrategyError(
                "Strategy '%s' does not declare three-way merge capability "
                "(set three_way_capable=True on a custom strategy to opt in)"
                % name)

        options = schema.get('mergeOptions')
        if options is None:
            options = {}
        elif not isinstance(options, dict):
            raise ThreeWayStrategyError(
                "mergeOptions for strategy '%s' must be an object" % name)

        return name, strategy, options

    def merge_leaf(self, ancestor, left, right, path=()):
        """Atomic three-way comparison of scalar/leaf values."""
        left_deleted = (not _is_undef(ancestor)) and _is_undef(left)
        right_deleted = (not _is_undef(ancestor)) and _is_undef(right)
        left_changed = left_deleted or (
            (not _is_undef(left)) and (
                _is_undef(ancestor) or left != ancestor))
        right_changed = right_deleted or (
            (not _is_undef(right)) and (
                _is_undef(ancestor) or right != ancestor))

        # Neither side touched the path.
        if not left_changed and not right_changed:
            return MISSING if _is_undef(ancestor) else ancestor

        # Only one side touched the path: accept its edit, including a
        # one-sided deletion or an independent addition.
        if left_changed and not right_changed:
            return MISSING if left_deleted else left
        if right_changed and not left_changed:
            return MISSING if right_deleted else right

        # Both sides changed. Identical edits (including identical deletions
        # or identical independent additions) are applied exactly once.
        if not (left_deleted or right_deleted) and left == right:
            return left
        if left_deleted and right_deleted:
            return MISSING

        ancestor_present = not _is_undef(ancestor)
        if not ancestor_present:
            conflict_type = 'add_add'
        elif left_deleted or right_deleted:
            conflict_type = 'delete_modify'
        else:
            conflict_type = 'different_modification'

        _conflict, resolved = self.register_conflict(
            path, conflict_type,
            MISSING if not ancestor_present else ancestor,
            MISSING if _is_undef(left) else left,
            MISSING if _is_undef(right) else right)
        return resolved

    def merge_objects(self, ancestor, left, right, schema, options, path):
        objcls = self.merger.objclass_menu.get(
            options.get('objClass', '_default'), dict)
        if objcls is None:
            raise ThreeWayStrategyError(
                "objClass '%s' not recognized" % options.get('objClass'))
        result = objcls()

        if _is_undef(ancestor):
            ancestor = objcls()
        if _is_undef(left):
            left = objcls()
        if _is_undef(right):
            right = objcls()

        keys = list(ancestor.keys())
        for document in (left, right):
            for key in document.keys():
                if key not in keys:
                    keys.append(key)

        for key in keys:
            sub_ancestor = ancestor.get(key, MISSING)
            sub_left = left.get(key, MISSING)
            sub_right = right.get(key, MISSING)

            sub_schema = MISSING
            if isinstance(schema, dict):
                sub_schema = self._subschema_for_property(schema, key)

            value = self.merge(
                sub_ancestor, sub_left, sub_right, sub_schema,
                path + (key,))
            if not _is_undef(value):
                result[key] = value

        return result

    def _subschema_for_property(self, schema, key):
        properties = schema.get('properties')
        if isinstance(properties, dict) and key in properties:
            return properties[key]

        pattern_properties = schema.get('patternProperties')
        if isinstance(pattern_properties, dict):
            for pattern, sub_schema in pattern_properties.items():
                if re.search(pattern, str(key)):
                    return sub_schema

        additional = schema.get('additionalProperties')
        if isinstance(additional, dict):
            return additional

        return {}

    def _resolve_id_ref(self, item, id_ref):
        if isinstance(id_ref, (list, tuple)):
            return tuple(self._resolve_single_pointer(item, ref)
                         for ref in id_ref)
        return self._resolve_single_pointer(item, id_ref)

    @staticmethod
    def _resolve_single_pointer(item, pointer):
        if not isinstance(pointer, str) or not pointer.startswith('/'):
            raise ThreeWayInputError(
                "idRef must be a JSON pointer starting with '/' (got %r)"
                % pointer)

        if pointer == '/':
            # The root pointer identifies the item itself, supporting arrays
            # of scalar values used as a set of unique values.
            return item

        current = item
        for raw_token in pointer.split('/')[1:]:
            token = raw_token.replace('~1', '/').replace('~0', '~')
            if isinstance(current, dict):
                if token not in current:
                    raise KeyError(token)
                current = current[token]
            elif isinstance(current, list):
                index = int(token)
                current = current[index]
            else:
                raise KeyError(token)
        return current

    def _index_by_id(self, array, id_ref, path):
        """Map id -> list of items. Duplicate ids are reported as conflicts."""
        indexed = OrderedDict()
        for position, item in enumerate(array):
            try:
                item_id = self._resolve_id_ref(item, id_ref)
            except (KeyError, IndexError, ValueError, TypeError):
                # Items without a resolvable stable identity cannot be matched
                # and are therefore treated as whole-value additions.
                indexed.setdefault(None, []).append((position, item))
                continue
            indexed.setdefault(item_id, []).append((position, item))
        return indexed

    def merge_array_by_id(self, ancestor, left, right, schema, options, path):
        id_ref = options['idRef']

        if _is_undef(ancestor):
            ancestor = []
        if _is_undef(left):
            left = []
        if _is_undef(right):
            right = []

        ancestor_map = self._index_by_id(ancestor, id_ref, path)
        left_map = self._index_by_id(left, id_ref, path)
        right_map = self._index_by_id(right, id_ref, path)

        blocked_ids = set()
        left_duplicate = False
        right_duplicate = False

        # A duplicated id inside a single side is structurally ambiguous and
        # must never be resolved by silently preferring one of the copies.
        for side_name, side_map in (
                (('left', left_map), ('right', right_map))):
            for item_id, entries in side_map.items():
                if item_id is None or len(entries) < 2:
                    continue
                blocked_ids.add(item_id)
                if side_name == 'left':
                    left_duplicate = True
                else:
                    right_duplicate = True
                self.report_conflict(
                    path + (entries[0][0],),
                    'duplicate_id',
                    MISSING if item_id not in ancestor_map
                    else ancestor_map[item_id][0][1],
                    MISSING if side_name != 'left'
                    else [entry[1] for entry in entries],
                    MISSING if side_name != 'right'
                    else [entry[1] for entry in entries],
                    message="Duplicate id %r in %s document"
                            % (item_id, side_name))

        sub_schema = {}
        if isinstance(schema, dict):
            items = schema.get('items')
            if isinstance(items, dict):
                sub_schema = items
            elif isinstance(items, list):
                raise ThreeWayStrategyError(
                    "arrayMergeById three-way merge is not supported when "
                    "'items' is an array")

        result = []
        ordered_ids = list(ancestor_map.keys())
        new_ids = []
        unidentifiable_seen = False
        for side_map in (left_map, right_map):
            for item_id in side_map.keys():
                if item_id is None:
                    unidentifiable_seen = True
                    continue
                if item_id not in ancestor_map and item_id not in new_ids:
                    new_ids.append(item_id)

        # Order new identities deterministically so that swapping left and
        # right produces an equivalent result. Identity values are JSON
        # scalars (or tuples of scalars for a compound idRef); fall back to
        # their string form when the types cannot be compared directly.
        try:
            new_ids = sorted(new_ids)
        except TypeError:
            new_ids = sorted(new_ids, key=lambda value: (
                type(value).__name__, repr(value)))

        ordered_ids.extend(new_ids)
        if unidentifiable_seen:
            ordered_ids.append(None)

        # Stable positional segment for conflict paths. Items present in the
        # ancestor keep their ancestor index; new ids take indices after the
        # ancestor array so a path never aliases a different item.
        path_indices = {}
        for item_id, entries in ancestor_map.items():
            if item_id is not None:
                path_indices[item_id] = entries[0][0]
        next_index = len(ancestor)
        for item_id in ordered_ids:
            if item_id not in path_indices:
                path_indices[item_id] = next_index
                next_index += 1

        for item_id in ordered_ids:
            if item_id in blocked_ids:
                continue

            ancestor_entries = ancestor_map.get(item_id, [])
            left_entries = left_map.get(item_id, [])
            right_entries = right_map.get(item_id, [])

            ancestor_item = ancestor_entries[0][1] if ancestor_entries \
                else MISSING
            left_item = left_entries[0][1] if left_entries else MISSING
            right_item = right_entries[0][1] if right_entries else MISSING

            if item_id is None:
                # Unidentifiable items have no common identity to match on.
                for source_item in (left_item, right_item):
                    if not _is_undef(source_item):
                        result.append(copy.deepcopy(source_item))
                continue

            # Identity is the idRef value, so an id change appears as the old
            # identity being deleted and a new one being added. Matching is
            # therefore purely identity-based: a side without this id has no
            # item for it, regardless of any new ids it introduced.
            item_path = path + (path_indices[item_id],)

            # Handle whole-item presence first: a deleted identity must not
            # be silently resurrected by field-level merging. Only when the
            # item exists on every side do we descend into its fields.
            left_present = not _is_undef(left_item)
            right_present = not _is_undef(right_item)
            ancestor_present = not _is_undef(ancestor_item)

            # A missing id only counts as an explicit deletion when that
            # side's identity mapping is trustworthy. A side containing a
            # duplicate id is structurally ambiguous, so its missing ids are
            # treated as untouched rather than as deletions.
            left_deleted = (ancestor_present and not left_present
                            and not left_duplicate)
            right_deleted = (ancestor_present and not right_present
                             and not right_duplicate)

            if ancestor_present and not (left_present and right_present):
                if not left_present and not right_present:
                    if left_deleted or right_deleted:
                        continue
                    # Both mappings are ambiguous: leave the ancestor item.
                    result.append(copy.deepcopy(ancestor_item))
                    continue

                present_item = left_item if left_present else right_item
                missing_is_left = not left_present

                if not (left_deleted or right_deleted):
                    # The absent side is merely ambiguous/untouched: keep the
                    # present item when it did not diverge from the ancestor.
                    result.append(copy.deepcopy(present_item))
                    continue

                # Decide whether the side without the identity explicitly
                # deleted it. If the present side's item is unchanged from the
                # ancestor, field-level merging below keeps the present copy
                # without fabricating a conflict against an untouched side.
                if present_item != ancestor_item:
                    # The present side modified the item while the other side
                    # no longer has this identity: a genuine delete/modify.
                    _conflict, resolved = self.register_conflict(
                        item_path, 'delete_modify',
                        ancestor_item,
                        MISSING if missing_is_left else left_item,
                        MISSING if not missing_is_left else right_item,
                        message="Item with id %r was deleted on one side and "
                                "modified on the other" % (item_id,))
                    if not _is_undef(resolved):
                        result.append(resolved)
                    continue

                # Present side kept the ancestor item verbatim: treat the
                # missing side as untouched rather than as an explicit delete
                # and keep the unchanged item.
                result.append(copy.deepcopy(ancestor_item))
                continue

            if not ancestor_present:
                if left_present and right_present:
                    if left_item == right_item:
                        result.append(copy.deepcopy(left_item))
                        continue
                    _conflict, resolved = self.register_conflict(
                        item_path, 'add_add',
                        MISSING,
                        MISSING if not left_present else left_item,
                        MISSING if not right_present else right_item,
                        message="Item with id %r was added differently on "
                                "the two sides" % (item_id,))
                    if not _is_undef(resolved):
                        result.append(resolved)
                    continue
                if left_present or right_present:
                    result.append(copy.deepcopy(
                        left_item if left_present else right_item))
                    continue

            value = self.merge(ancestor_item, left_item, right_item,
                               sub_schema, item_path)
            if not _is_undef(value):
                result.append(value)

        return result

    def merge(self, ancestor, left, right, schema, path=()):
        name, strategy, options = self.strategy_for(schema)

        if name is not None and not isinstance(
                strategy,
                (builtin_strategies.ObjectMerge,
                 builtin_strategies.ArrayMergeById)):
            # Any other three-way-capable strategy controls the node itself.
            return strategy.merge_three_way(
                self, ancestor, left, right, schema, path=path, **options)

        if name == 'arrayMergeById':
            if not self._all_type(ancestor, left, right, list):
                conflict_type = 'different_modification'
                self.report_conflict(
                    path, conflict_type,
                    MISSING if _is_undef(ancestor) else ancestor,
                    MISSING if _is_undef(left) else left,
                    MISSING if _is_undef(right) else right,
                    message="arrayMergeById applied to a non-array value")
                return MISSING
            if 'idRef' not in options:
                # No stable identity was explicitly declared: never guess
                # pairing (neither by index nor by an implicit 'id' field),
                # treat the whole array as an atomic value.
                return self.merge_leaf(ancestor, left, right, path)
            return self.merge_array_by_id(
                ancestor, left, right, schema, options, path)

        if name == 'objectMerge' or name is None:
            if self._all_type(ancestor, left, right, dict):
                return self.merge_objects(
                    ancestor, left, right, schema, options, path)
            if name == 'objectMerge' and not self._all_undef(
                    ancestor, left, right):
                self.report_conflict(
                    path, 'different_modification',
                    MISSING if _is_undef(ancestor) else ancestor,
                    MISSING if _is_undef(left) else left,
                    MISSING if _is_undef(right) else right,
                    message="objectMerge applied to a non-object value")
                return MISSING
            # Default strategy: fall back to atomic leaf semantics.
            return self.merge_leaf(ancestor, left, right, path)

        # Unreachable: strategy_for already rejected unknown strategies.
        raise ThreeWayStrategyError(
            "Strategy '%s' has no three-way implementation" % name)

    @staticmethod
    def _all_type(ancestor, left, right, expected_type):
        return all(isinstance(value, expected_type)
                   for value in (ancestor, left, right)
                   if not _is_undef(value))

    @staticmethod
    def _all_undef(*values):
        return all(_is_undef(value) for value in values)


class ThreeWayMerger(object):
    """Three-way merge configured by a merge schema.

    The schema uses the same ``mergeStrategy`` / ``mergeOptions`` keywords as
    the two-way :class:`jsonmerge.Merger`. A strategy is only used if it
    explicitly declares three-way capability; unknown or non-capable
    strategies make the constructor/merge fail rather than silently picking a
    side.
    """

    def __init__(self, schema, strategies=(), objclass_def='dict',
                 objclass_menu=None, validatorclass=Draft4Validator):
        if not isinstance(schema, dict):
            raise ThreeWayInputError("merge schema must be a JSON object")

        self.schema = copy.deepcopy(schema)

        if hasattr(validatorclass, 'ID_OF'):
            resolver = LocalRefResolver.from_schema(
                self.schema, id_of=validatorclass.ID_OF)
        else:
            resolver = LocalRefResolver.from_schema(self.schema)
        self.resolver = resolver
        self.validatorclass = validatorclass
        self.validator = validatorclass(self.schema, resolver=resolver)

        self.strategies = dict(ThreeWayMerger.builtin_strategies())
        if isinstance(strategies, dict):
            self.strategies.update(strategies)
        else:
            for name, strategy in strategies:
                self.strategies[name] = strategy

        self.objclass_menu = {'dict': dict, 'OrderedDict': OrderedDict}
        if objclass_menu:
            self.objclass_menu.update(objclass_menu)
        if objclass_def not in self.objclass_menu:
            raise ThreeWayInputError(
                "Unknown default object class '%s'" % objclass_def)
        self.objclass_menu['_default'] = self.objclass_menu[objclass_def]

        self._validate_schema_strategies(self.schema)
        self._validate_object_classes(self.schema)

    def _validate_object_classes(self, schema, seen_refs=None):
        if seen_refs is None:
            seen_refs = set()
        if not isinstance(schema, dict):
            return

        if '$ref' in schema:
            ref = schema['$ref']
            if ref in seen_refs:
                return
            seen_refs.add(ref)
            try:
                with self.resolver.resolving(ref) as resolved:
                    self._validate_object_classes(resolved, seen_refs)
            except Exception:
                pass
            return

        if schema.get('mergeStrategy') == 'objectMerge':
            options = schema.get('mergeOptions')
            if isinstance(options, dict):
                obj_class = options.get('objClass', '_default')
                if obj_class not in self.objclass_menu:
                    raise ThreeWayStrategyError(
                        "objClass '%s' not recognized" % obj_class)

        for key in ('properties', 'patternProperties'):
            sub = schema.get(key)
            if isinstance(sub, dict):
                for sub_schema in sub.values():
                    self._validate_object_classes(sub_schema, seen_refs)

        additional = schema.get('additionalProperties')
        if isinstance(additional, dict):
            self._validate_object_classes(additional, seen_refs)

        items = schema.get('items')
        if isinstance(items, dict):
            self._validate_object_classes(items, seen_refs)
        elif isinstance(items, list):
            for sub_schema in items:
                self._validate_object_classes(sub_schema, seen_refs)

    def validate_document(self, document):
        """Validate a merged document against the merge schema.

        Raises :class:`ThreeWayInputError` with the first validation error.
        A document that is entirely absent (``None`` at the root) is accepted
        when the schema does not itself require a value.
        """
        if document is None:
            return

        errors = sorted(self.validator.iter_errors(document),
                        key=lambda error: list(error.absolute_path))
        if errors:
            first = errors[0]
            location = '/' + '/'.join(
                _escape_pointer_token(str(part))
                for part in first.absolute_path)
            raise ThreeWayInputError(
                "Merged document fails schema validation at %s: %s"
                % (location or '#', first.message))

    @staticmethod
    def builtin_strategies():
        return {
            "overwrite": builtin_strategies.Overwrite(),
            "objectMerge": builtin_strategies.ObjectMerge(),
            "arrayMergeById": builtin_strategies.ArrayMergeById(),
        }

    def _validate_schema_strategies(self, schema, seen_refs=None):
        if seen_refs is None:
            seen_refs = set()
        if not isinstance(schema, dict):
            return

        if '$ref' in schema:
            ref = schema['$ref']
            if ref in seen_refs:
                return
            seen_refs.add(ref)
            try:
                with self.resolver.resolving(ref) as resolved:
                    self._validate_schema_strategies(resolved, seen_refs)
            except Exception:
                # Broken references are reported by the JSON schema validator;
                # strategy validation only needs to inspect local keywords.
                pass
            return

        name = schema.get('mergeStrategy')
        if name is not None:
            if name not in self.strategies:
                raise ThreeWayStrategyError(
                    "Unknown strategy '%s' in merge schema" % name)
            if not getattr(self.strategies[name], 'three_way_capable', False):
                raise ThreeWayStrategyError(
                    "Strategy '%s' does not declare three-way merge "
                    "capability" % name)

        for key in ('properties', 'patternProperties'):
            sub = schema.get(key)
            if isinstance(sub, dict):
                for sub_schema in sub.values():
                    self._validate_schema_strategies(sub_schema, seen_refs)

        additional = schema.get('additionalProperties')
        if isinstance(additional, dict):
            self._validate_schema_strategies(additional, seen_refs)

        items = schema.get('items')
        if isinstance(items, dict):
            self._validate_schema_strategies(items, seen_refs)
        elif isinstance(items, list):
            for sub_schema in items:
                self._validate_schema_strategies(sub_schema, seen_refs)

    def merge(self, ancestor, left, right, validate=False):
        """Run a three-way merge.

        Inputs are never modified: they are deep-copied before merging.
        When ``validate`` is true, a conflict-free result is additionally
        checked against the JSON schema and a
        :class:`ThreeWayInputError` is raised on failure.
        """
        ancestor_doc = MISSING if ancestor is None else copy.deepcopy(ancestor)
        left_doc = MISSING if left is None else copy.deepcopy(left)
        right_doc = MISSING if right is None else copy.deepcopy(right)

        walk = _ThreeWayWalk(self)
        merged = walk.merge(
            MISSING if ancestor is None else ancestor_doc,
            left_doc, right_doc, self.schema)

        if _is_undef(merged):
            merged = None

        if validate and not walk.conflicts:
            self.validate_document(merged)

        return ThreeWayMergeResult(merged, walk.conflicts)

    def resolve(self, ancestor, left, right, resolutions, validate=True):
        """Re-run the merge and apply caller-supplied conflict resolutions.

        ``resolutions`` maps conflict ids to the value to use at the conflict
        path. Pass :data:`MISSING` to request deletion; an explicit ``None``
        is stored as a JSON null value. The whole document is rebuilt from
        scratch and re-validated, so any conflict that is not resolved is
        reported again. When ``validate`` is true (the default), a fully
        resolved document is also checked against the JSON schema.
        """
        if not isinstance(resolutions, dict):
            raise ThreeWayConflictResolutionError(
                "resolutions must be a dict mapping conflict ids to values")

        result = self.merge(ancestor, left, right)

        known_ids = {conflict.conflict_id for conflict in result.conflicts}
        unknown = sorted(set(resolutions) - known_ids)
        if unknown:
            raise ThreeWayConflictResolutionError(
                "Unknown conflict id(s): %s" % ', '.join(unknown))

        if not result.has_conflicts:
            return result

        overrides = {}
        for conflict in result.conflicts:
            if conflict.conflict_id in resolutions:
                value = resolutions[conflict.conflict_id]
                overrides[conflict.path] = (
                    MISSING if value is MISSING else copy.deepcopy(value))

        ancestor_doc = MISSING if ancestor is None else copy.deepcopy(ancestor)
        left_doc = MISSING if left is None else copy.deepcopy(left)
        right_doc = MISSING if right is None else copy.deepcopy(right)

        walk = _ResolvingThreeWayWalk(self, overrides)
        merged = walk.merge(ancestor_doc, left_doc, right_doc, self.schema)

        if _is_undef(merged):
            merged = None

        if validate and not walk.conflicts:
            self.validate_document(merged)

        return ThreeWayMergeResult(merged, walk.conflicts)


class _ResolvingThreeWayWalk(_ThreeWayWalk):
    """Walk that substitutes caller-supplied values at conflict paths."""

    def __init__(self, merger, overrides):
        super(_ResolvingThreeWayWalk, self).__init__(merger)
        self.overrides = overrides

    def register_conflict(self, path, conflict_type, ancestor, left, right,
                          message=None):
        pointer = path_to_pointer(path)
        if pointer in self.overrides:
            value = self.overrides[pointer]
            return None, value
        return super(_ResolvingThreeWayWalk, self).register_conflict(
            path, conflict_type, ancestor, left, right, message)

    def merge(self, ancestor, left, right, schema, path=()):
        pointer = path_to_pointer(path)
        if pointer in self.overrides:
            value = self.overrides[pointer]
            if _is_undef(value):
                return MISSING
            return copy.deepcopy(value)
        return super(_ResolvingThreeWayWalk, self).merge(
            ancestor, left, right, schema, path)


def three_way_merge(ancestor, left, right, schema=None, strategies=(),
                    validate=False):
    """Merge two documents diverged from a common ancestor.

    Returns a :class:`ThreeWayMergeResult` with the merged document and a
    structured conflict list. Unrelated object fields are combined, identical
    edits are kept once and divergent edits are reported instead of overwritten.
    """
    if schema is None:
        schema = {}
    merger = ThreeWayMerger(schema, strategies=strategies)
    return merger.merge(ancestor, left, right, validate=validate)
