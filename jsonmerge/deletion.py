# vim:ts=4 sw=4 expandtab softtabstop=4
"""Explicit, opt-in deletion protocol for incremental merges.

The protocol is enabled per strategy through the ``enableDelete`` merge
option.  When disabled (the default), documents are merged exactly as
before: the marker objects defined here are ordinary JSON values and no
special interpretation takes place.

Two markers are recognized.  They are deliberately distinct from any
plain JSON value so that a legitimate ``None``/``null`` keeps working:

* an object member is deleted with an object that carries a single
  property named by :data:`DELETE_KEY`::

      {"__delete__": null}

* an *arrayMergeById* member is deleted by an object that only carries
  the array item ID and the same marker property, e.g.::

      {"id": "A", "__delete__": null}

User data that happens to look like a marker can be preserved verbatim
by wrapping it in the escape object named by :data:`ESCAPE_KEY`::

      {"__escaped__": {"__delete__": null}}
"""

from jsonmerge.exceptions import HeadInstanceError, SchemaError
from jsonmerge.jsonvalue import JSONValue

DELETE_KEY = "__delete__"
ESCAPE_KEY = "__escaped__"

# Property added to a version history entry recording a deletion.  It is an
# output of the version strategy, never an input marker, so it does not
# collide with the ordinary 'value' property of a history entry.
DELETED_KEY = "deleted"

# The only value accepted inside a marker object.  A marker with any other
# payload is a malformed marker and is rejected before the merge.
MARKER_VALUE = None


class DeletionOptions(object):
    """Validated deletion settings for a single strategy invocation."""

    def __init__(self, enableDelete=False, strictDelete=False):
        self.enable = bool(enableDelete)
        self.strict = bool(strictDelete)


def resolve(schema, **kwargs):
    """Build deletion options from strategy kwargs, validating consistency."""
    enable = bool(kwargs.get("enableDelete", False))
    strict = bool(kwargs.get("strictDelete", False))

    if strict and not enable:
        raise SchemaError(
            "'strictDelete' option requires 'enableDelete' to be enabled",
            schema)

    return DeletionOptions(enableDelete=enable, strictDelete=strict)


def is_marker_object(val):
    """Return True for a plain object that carries the delete property."""
    return isinstance(val, dict) and DELETE_KEY in val


def is_delete_marker(val):
    """Object member delete marker: ``{"__delete__": null}`` and nothing else."""
    return (is_marker_object(val)
            and len(val) == 1
            and val[DELETE_KEY] is MARKER_VALUE)


def is_member_delete_marker(val):
    """Array member delete marker.

    It carries the item ID plus the marker property, e.g.
    ``{"id": "A", "__delete__": null}``.  The ID field itself is strategy
    specific (see the idRef option), so here we only require that the marker
    payload is null and that at least one identifying property is present.
    """
    return (is_marker_object(val)
            and len(val) == 2
            and val[DELETE_KEY] is MARKER_VALUE)


def is_array_delete_marker(val):
    """Any delete marker allowed as an array member.

    Both the one-property form (implicit position for *arrayMergeByIndex*)
    and the id-plus-marker form (*arrayMergeById*) are accepted.  Whether
    the target can actually be resolved is checked by the strategy.
    """
    return (is_marker_object(val)
            and len(val) <= 2
            and val[DELETE_KEY] is MARKER_VALUE)


def is_escape_object(val):
    return isinstance(val, dict) and ESCAPE_KEY in val and len(val) == 1


def unescape(val):
    """Remove one layer of escape wrapping from a head value."""
    if is_escape_object(val):
        return val[ESCAPE_KEY]
    return val


def _malformed(value, message):
    raise HeadInstanceError(message, value)


def validate_marker(value):
    """Reject objects that look like markers but are malformed.

    Called for every object in a head subtree while the deletion protocol
    is enabled.  Validation runs before any data is modified, so a malformed
    marker aborts the whole merge.

    Both object member markers (one property) and array member markers (an
    identifying property plus the marker) are accepted; the strategies check
    the exact shape relevant to them.
    """
    assert isinstance(value, JSONValue)

    val = value.val
    if not isinstance(val, dict):
        return

    if ESCAPE_KEY in val:
        if len(val) != 1:
            _malformed(value,
                       "Escape marker '%s' must be the only property"
                       % ESCAPE_KEY)
        return

    if DELETE_KEY in val:
        if len(val) > 2:
            _malformed(value,
                       "Delete marker '%s' may only carry the marker and at "
                       "most one identifying property" % DELETE_KEY)
        if val[DELETE_KEY] is not MARKER_VALUE:
            _malformed(value,
                       "Delete marker '%s' must have a null value"
                       % DELETE_KEY)


def prevalidate(value):
    """Walk a head subtree and reject every malformed marker up front."""
    assert isinstance(value, JSONValue)

    if value.is_undef():
        return

    validate_marker(value)

    val = value.val
    if isinstance(val, dict):
        for _key, child in value.items():
            prevalidate(child)
    elif isinstance(val, list):
        for child in value:
            prevalidate(child)
