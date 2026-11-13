# vim:ts=4 sw=4 expandtab softtabstop=4
"""Explicit delete/escape protocol for incremental merges.

The protocol is opt-in per strategy through the ``enableDelete`` merge
option.  When enabled, two control markers may appear in *head* documents:

* a delete marker marks an object property (or an array item identified by
  its ID) for removal;
* an escape marker stores user data that literally looks like a marker.

Markers are control information.  They are interpreted before any merge
takes place and never appear in merge output.
"""

from jsonmerge.exceptions import MarkerError, SchemaError
from jsonmerge.jsonvalue import JSONValue

#: Default keyword used for the delete marker.
DEFAULT_DELETE_MARKER = "$delete"

#: Fixed keyword used for the escape marker.
ESCAPE_MARKER = "$escape"

#: Merge option names understood by the protocol.
ENABLE_OPTION = "enableDelete"
MARKER_OPTION = "deleteMarker"
STRICT_OPTION = "strictDelete"


def is_enabled(merge_options):
    return bool(merge_options.get(ENABLE_OPTION, False))


def delete_marker_key(merge_options):
    return merge_options.get(MARKER_OPTION, DEFAULT_DELETE_MARKER)


def validate_options(merge_options, schema):
    """Validate delete-protocol merge options before touching any data."""
    key = merge_options.get(MARKER_OPTION, DEFAULT_DELETE_MARKER)
    if not isinstance(key, str) or not key:
        raise SchemaError(
            "'%s' merge option must be a non-empty string" % MARKER_OPTION,
            schema)
    if key == ESCAPE_MARKER:
        raise SchemaError(
            "'%s' merge option can't be set to '%s', which is reserved "
            "for the escape marker" % (MARKER_OPTION, ESCAPE_MARKER),
            schema)


def _is_object(jv):
    return isinstance(jv.val, dict)


def _head_key(jv, key):
    return JSONValue(jv.val[key], jv.ref + "/" + jv._ref_escape(key))


def classify_object(head, merge_options):
    """Classify an object-level head value.

    Returns a ``(kind, payload)`` tuple:

    * ``("value", head)`` -- ordinary data, no marker present;
    * ``("delete", None)`` -- well-formed delete marker;
    * ``("escape", JSONValue)`` -- escaped user data.
    """
    delete_key = delete_marker_key(merge_options)

    if not _is_object(head):
        return "value", head

    has_delete = delete_key in head.val
    has_escape = ESCAPE_MARKER in head.val

    if has_delete and has_escape:
        raise MarkerError(
            "Object contains both '%s' and '%s' control markers"
            % (delete_key, ESCAPE_MARKER), head)

    if has_delete:
        if head.val[delete_key] is not True:
            raise MarkerError(
                "Malformed delete marker: '%s' must be true" % delete_key,
                head)
        if len(head.val) != 1:
            raise MarkerError(
                "Malformed delete marker: no other properties are allowed "
                "alongside '%s'" % delete_key, head)
        return "delete", None

    if has_escape:
        if len(head.val) != 1:
            raise MarkerError(
                "Malformed escape marker: no other properties are allowed "
                "alongside '%s'" % ESCAPE_MARKER, head)
        return "escape", _head_key(head, ESCAPE_MARKER)

    return "value", head


def classify_array_item(item, merge_options):
    """Classify an array item in the head of arrayMergeById.

    The item's ID is resolved by the strategy, since it depends on
    *idRef*.  Returns a ``(kind, payload)`` tuple:

    * ``("value", item)`` -- ordinary array item;
    * ``("delete", item)`` -- delete marker (flag validated, the strategy
      resolves and checks the ID);
    * ``("escape", JSONValue)`` -- escaped array item, stored verbatim.
    """
    delete_key = delete_marker_key(merge_options)

    if not _is_object(item):
        return "value", item

    has_delete = delete_key in item.val
    has_escape = ESCAPE_MARKER in item.val

    if has_delete and has_escape:
        raise MarkerError(
            "Array item contains both '%s' and '%s' control markers"
            % (delete_key, ESCAPE_MARKER), item)

    if has_escape:
        if len(item.val) != 1:
            raise MarkerError(
                "Malformed escape marker: no other properties are allowed "
                "alongside '%s'" % ESCAPE_MARKER, item)
        return "escape", _head_key(item, ESCAPE_MARKER)

    if has_delete:
        if item.val[delete_key] is not True:
            raise MarkerError(
                "Malformed delete marker: '%s' must be true" % delete_key,
                item)
        return "delete", item

    return "value", item


def validate_array_delete_item(item, merge_options, id_ref):
    """Reject array delete markers carrying payload or missing their ID."""
    delete_key = delete_marker_key(merge_options)

    if isinstance(id_ref, list):
        id_keys = set(
            ref.lstrip("/").split("/")[0]
            for ref in id_ref if isinstance(ref, str) and ref.startswith("/"))
    elif isinstance(id_ref, str) and id_ref == "id":
        id_keys = {"id"}
    elif isinstance(id_ref, str) and id_ref.startswith("/") and id_ref != "/":
        id_keys = {id_ref.lstrip("/").split("/")[0]}
    elif isinstance(id_ref, str) and id_ref != "/":
        # A relative JSON pointer fragment names a property directly.
        id_keys = {id_ref.split("/")[0]}
    else:
        id_keys = set()

    extra = set(item.val.keys()) - {delete_key} - id_keys
    if extra:
        raise MarkerError(
            "Malformed delete marker: unexpected properties %s"
            % sorted(extra), item)

    if not id_keys:
        raise MarkerError(
            "Malformed delete marker: idRef %r does not name a property "
            "that can be used in a delete marker" % (id_ref,), item)


def reject_unsupported(walk, schema, merge_options):
    """Fail early when the delete protocol is requested on a strategy that
    does not support it, instead of silently leaking markers into output."""
    if is_enabled(merge_options):
        raise SchemaError(
            "Delete protocol ('%s') is not supported by this merge "
            "strategy" % ENABLE_OPTION, schema)
