# vim:ts=4 sw=4 expandtab softtabstop=4
"""Explicit migration plans for merge strategy changes.

When a merge schema evolves (e.g. a field switches from the 'version'
strategy to 'objectMerge'), documents produced with the old schema no
longer match the structure the new schema expects.  Simply swapping the
schema would make the merger interpret old historical structures (such
as version arrays) as new business values.

This module implements an explicit, approve-then-apply migration plan:

* ``plan_migration(old_schema, new_schema, document)`` compares the two
  schemas, identifies paths whose strategy or key options changed and
  builds a :class:`MigrationPlan`.  The plan records digests of the old
  schema and the existing merge product, plus a human readable note
  about any history each step will discard.

* Automatic steps are only generated for unambiguous conversions:
  replacing a 'version' array with one explicitly selected entry.  The
  caller must name the entry (by index); the plan never silently picks
  the last (or any other) entry on its own.  Every other change
  requires a caller-provided transform function.

* The plan must be approved before it can be applied, expires after a
  configurable TTL and verifies that the document it is applied to is
  the same one it was planned for.  On any failure ``apply()`` raises
  and never returns a half-migrated object.

* The migrated document is validated against the schema produced by
  ``Merger(new_schema).get_schema()`` so that it can be used directly
  as the base of the next ``merge()`` call.
"""

import copy
import hashlib
import json
import time

import jsonschema

from jsonmerge import Merger
from jsonmerge.exceptions import JSONMergeError


class MigrationError(JSONMergeError):
    """A migration plan could not be built or applied."""


class PlanNotApprovedError(MigrationError):
    """apply() was called on a plan that was not approved yet."""


class PlanExpiredError(MigrationError):
    """apply() was called after the plan expired."""


# Options that change how a strategy interprets the stored document.
# Changes to these are treated like strategy changes: they need an
# explicit decision from the caller.
KEY_OPTIONS = {
    'arrayMergeById': frozenset(['idRef']),
}

# Keywords that make a schema default to the 'objectMerge' strategy
# (mirrors WalkSchema.schema_is_object).
_OBJECT_ONLY_KEYWORDS = (
    'maxProperties',
    'minProperties',
    'required',
    'additionalProperties',
    'properties',
    'patternProperties',
    'dependencies',
)


def _canonical_digest(value):
    serialized = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _default_strategy(schema):
    for keyword in _OBJECT_ONLY_KEYWORDS:
        if keyword in schema:
            return 'objectMerge'
    if schema.get('type') == 'object':
        return 'objectMerge'
    return 'overwrite'


def _effective_strategy(schema):
    return schema.get('mergeStrategy') or _default_strategy(schema)


def _diff_schemas(old, new, path=''):
    """Yield (path, old_schema, new_schema) for nodes whose strategy or
    key options changed."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return

    old_strategy = _effective_strategy(old)
    new_strategy = _effective_strategy(new)
    old_options = old.get('mergeOptions') or {}
    new_options = new.get('mergeOptions') or {}

    changed = old_strategy != new_strategy
    if not changed:
        key_options = KEY_OPTIONS.get(old_strategy, frozenset()) | \
                KEY_OPTIONS.get(new_strategy, frozenset())
        for option in key_options:
            if old_options.get(option) != new_options.get(option):
                changed = True
                break

    if changed:
        yield (path, old, new)

    old_properties = old.get('properties') or {}
    new_properties = new.get('properties') or {}
    for name in old_properties:
        if name in new_properties:
            subpath = '%s/%s' % (
                    path, name.replace('~', '~0').replace('/', '~1'))
            for change in _diff_schemas(old_properties[name],
                                        new_properties[name], subpath):
                yield change

    old_items = old.get('items')
    new_items = new.get('items')
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        for change in _diff_schemas(old_items, new_items, path + '/[*]'):
            yield change


def _split_path(path):
    if not path:
        return []
    return [segment.replace('~1', '/').replace('~0', '~')
            for segment in path.split('/')[1:]]


class _Absent(object):
    pass


_ABSENT = _Absent()


def _get_at_path(document, segments):
    """Return value at path, or _ABSENT if not present.

    '[*]' segments look into the first element of a list; this is
    only used to compute human readable summaries."""
    value = document
    for segment in segments:
        if segment == '[*]' and isinstance(value, list):
            if not value:
                return _ABSENT
            value = value[0]
        elif isinstance(value, dict) and segment in value:
            value = value[segment]
        else:
            return _ABSENT
    return value


def _transform_at_path(value, segments, func, path):
    """Return a copy of value with func applied at the given path.
    '[*]' segments apply func to every element of a list."""
    if not segments:
        return func(value)

    segment = segments[0]

    if segment == '[*]':
        if not isinstance(value, list):
            raise MigrationError(
                "Expected an array at '%s' while migrating" % path)
        return [_transform_at_path(item, segments[1:], func, path)
                for item in value]

    if isinstance(value, dict) and segment in value:
        value = dict(value)
        value[segment] = _transform_at_path(
                value[segment], segments[1:], func, path)
        return value

    # Path not present in the document: no history to migrate.
    return value


class MigrationChange(object):
    """One detected strategy (or key option) change at a path."""

    KIND_AUTOMATIC = 'automatic'
    KIND_TRANSFORM = 'transform'
    KIND_PENDING = 'pending'

    def __init__(self, path, old_strategy, new_strategy,
                 old_options, new_options, kind, description, discarded):
        self.path = path
        self.old_strategy = old_strategy
        self.new_strategy = new_strategy
        self.old_options = old_options
        self.new_options = new_options
        self.kind = kind
        self.description = description
        self.discarded = discarded

    def __repr__(self):
        return "MigrationChange(%r, %r -> %r, %s)" % (
                self.path, self.old_strategy, self.new_strategy, self.kind)


class MigrationPlan(object):
    """A migration plan produced by plan_migration().

    Inspect ``changes`` and ``pending``, then call ``approve()`` and
    finally ``apply(document)`` before the plan expires.
    """

    def __init__(self, old_schema, new_schema, changes, pending,
                 document_digest, ttl_seconds, clock):
        self.old_schema_digest = _canonical_digest(old_schema)
        self.new_schema_digest = _canonical_digest(new_schema)
        self.document_digest = document_digest
        self.changes = changes
        self.pending = pending
        self.created_at = clock()
        self.expires_at = self.created_at + ttl_seconds
        self.approved = False
        self._clock = clock
        # get_schema() mutates nested dicts of the schema it is called
        # on, so keep a private deep copy and never touch the caller's
        # schema object.
        self._new_schema = copy.deepcopy(new_schema)
        self._steps = []
        self._applied = False

    def summary(self):
        lines = [
            "migration plan:",
            "  old schema digest: %s" % self.old_schema_digest,
            "  new schema digest: %s" % self.new_schema_digest,
            "  document digest:   %s" % self.document_digest,
            "  expires at:        %.3f" % self.expires_at,
        ]
        for change in self.changes:
            lines.append("  [%s] %s: %s -> %s" % (
                change.kind, change.path or '/',
                change.old_strategy, change.new_strategy))
            lines.append("      %s" % change.description)
            if change.discarded:
                lines.append("      discards: %s" % change.discarded)
        for change in self.pending:
            lines.append("  [pending] %s: %s -> %s: %s" % (
                change.path or '/', change.old_strategy,
                change.new_strategy, change.description))
        return '\n'.join(lines)

    def approve(self):
        """Approve the plan.  Fails if some changes still lack an
        explicit conversion decision."""
        if self.pending:
            paths = ', '.join("'%s'" % (c.path or '/',)
                              for c in self.pending)
            raise MigrationError(
                "Migration plan has undecided changes at %s; provide "
                "'select_versions' or 'transforms' for these paths" % paths)
        self.approved = True

    def apply(self, document):
        """Apply the plan to the document it was planned for.

        Returns the migrated document.  Raises on any failure and never
        returns a half-migrated object; the passed document is not
        modified.
        """
        if not self.approved:
            raise PlanNotApprovedError(
                "Migration plan must be approved before it is applied")

        if self._clock() >= self.expires_at:
            raise PlanExpiredError(
                "Migration plan expired at %.3f" % self.expires_at)

        if self._applied:
            raise MigrationError("Migration plan was already applied")

        if _canonical_digest(document) != self.document_digest:
            raise MigrationError(
                "Document does not match the one this plan was made for")

        migrated = copy.deepcopy(document)
        try:
            for change, step in self._steps:
                segments = _split_path(change.path)
                migrated = _transform_at_path(
                        migrated, segments, step, change.path or '/')
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                "Migration step for '%s' failed: %s" % (
                    change.path or '/', exc))

        result_schema = Merger(copy.deepcopy(self._new_schema)).get_schema()
        try:
            jsonschema.Draft4Validator(result_schema).validate(migrated)
        except jsonschema.ValidationError as exc:
            raise MigrationError(
                "Migrated document does not validate against the schema "
                "produced by the new merge schema: %s" % exc.message)

        self._applied = True
        return migrated


def _make_version_step(selector, path):
    if isinstance(selector, bool) or not isinstance(selector, int):
        raise MigrationError(
            "Version selector for '%s' must be an integer index, got %r. "
            "Select the version to keep explicitly." % (path, selector))

    def step(value):
        if not isinstance(value, list):
            raise MigrationError(
                "Expected a version array at '%s', got %s. Value not "
                "previously produced by the 'version' strategy?" % (
                    path, type(value).__name__))
        try:
            entry = value[selector]
        except IndexError:
            raise MigrationError(
                "Version index %d out of range at '%s' (%d entries)" % (
                    selector, path, len(value)))
        if not isinstance(entry, dict) or 'value' not in entry:
            raise MigrationError(
                "Selected version entry at '%s' has no 'value' property. "
                "Value not previously produced by the 'version' "
                "strategy?" % path)
        return copy.deepcopy(entry['value'])

    return step


def _describe_version_discard(value, selector):
    if isinstance(value, list):
        kept = selector if selector >= 0 else len(value) + selector
        if 0 <= kept < len(value):
            return ("%d of %d version entries will be discarded; "
                    "only entry at index %d is kept" % (
                        len(value) - 1, len(value), kept))
        return ("all %d version entries are invalid for selector %d; "
                "step will fail" % (len(value), selector))
    return "no version history present at this path"


def plan_migration(old_schema, new_schema, document,
                   transforms=None, select_versions=None,
                   ttl_seconds=300, clock=time.time):
    """Build a migration plan from old_schema to new_schema for document.

    old_schema -- merge schema the document was produced with.
    new_schema -- merge schema the document should be migrated to.
    document -- existing merge product (not modified).
    transforms -- dict mapping a changed path to a function that takes
        the old value and returns the migrated value.  Required for any
        change that has no unambiguous automatic conversion.
    select_versions -- dict mapping a changed path to an integer index
        selecting which entry of a 'version' array to keep.  This is
        the only built-in automatic conversion and always requires an
        explicit choice; the plan never defaults to the last entry.
    ttl_seconds -- how long the plan stays valid.  0 means the plan is
        already expired.
    clock -- time source, mostly for testing.

    Returns a MigrationPlan.  The plan keeps digests of the old schema
    and the document, and a description of discarded history for each
    change.  Call approve() and then apply(document) before expiry.
    """
    if transforms is None:
        transforms = {}
    if select_versions is None:
        select_versions = {}

    changes = []
    pending = []
    steps = []
    decided_paths = set()

    for path, old, new in _diff_schemas(old_schema, new_schema):
        old_strategy = _effective_strategy(old)
        new_strategy = _effective_strategy(new)
        old_options = old.get('mergeOptions') or {}
        new_options = new.get('mergeOptions') or {}
        segments = _split_path(path)
        value = _get_at_path(document, segments)

        decided_paths.add(path)

        if path in transforms:
            transform = transforms[path]
            if not callable(transform):
                raise MigrationError(
                    "Transform for '%s' is not callable" % path)
            description = ("caller-provided transform converts the "
                           "value for the new '%s' strategy" % new_strategy)
            discarded = ("value at '%s' produced by the '%s' strategy "
                         "is handed to the transform; its previous "
                         "structure is not preserved" % (
                             path or '/', old_strategy))
            change = MigrationChange(
                    path, old_strategy, new_strategy, old_options,
                    new_options, MigrationChange.KIND_TRANSFORM,
                    description, discarded)
            changes.append(change)
            steps.append((change, transform))

        elif old_strategy == 'version' and new_strategy != 'version':
            if path not in select_versions:
                description = (
                    "a 'version' history must be collapsed to a single "
                    "value; provide select_versions['%s'] with the index "
                    "of the version to keep, or a transform. No version "
                    "is selected automatically" % path)
                change = MigrationChange(
                        path, old_strategy, new_strategy, old_options,
                        new_options, MigrationChange.KIND_PENDING,
                        description,
                        "undecided: entire version history at '%s'" % (
                            path or '/',))
                pending.append(change)
                continue

            selector = select_versions[path]
            step = _make_version_step(selector, path or '/')
            if value is _ABSENT:
                discarded = "no version history present at this path"
            else:
                discarded = _describe_version_discard(value, selector)
            description = ("keep explicitly selected version %d and "
                           "discard the rest of the history" % selector)
            change = MigrationChange(
                    path, old_strategy, new_strategy, old_options,
                    new_options, MigrationChange.KIND_AUTOMATIC,
                    description, discarded)
            changes.append(change)
            steps.append((change, step))

        else:
            description = (
                "no unambiguous automatic conversion from '%s' to '%s'; "
                "provide transforms['%s'] to convert the existing "
                "value" % (old_strategy, new_strategy, path))
            change = MigrationChange(
                    path, old_strategy, new_strategy, old_options,
                    new_options, MigrationChange.KIND_PENDING,
                    description,
                    "undecided: value at '%s' produced by the '%s' "
                    "strategy" % (path or '/', old_strategy))
            pending.append(change)

    unknown = (set(transforms) | set(select_versions)) - decided_paths
    if unknown:
        raise MigrationError(
            "Decisions given for paths with no detected change: %s" %
            ', '.join(sorted(repr(p) for p in unknown)))

    plan = MigrationPlan(
            old_schema, new_schema, changes, pending,
            _canonical_digest(document), ttl_seconds, clock)
    plan._steps = steps
    return plan
