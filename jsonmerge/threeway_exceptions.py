# vim:ts=4 sw=4 expandtab softtabstop=4


class ThreeWayMergeError(Exception):
    """Base class for errors raised by the three-way merge entry point."""


class ThreeWayStrategyError(ThreeWayMergeError):
    """Raised when a strategy cannot be used for a three-way merge."""


class ThreeWayConflictResolutionError(ThreeWayMergeError):
    """Raised when a caller-supplied conflict resolution is invalid."""


class ThreeWayInputError(ThreeWayMergeError):
    """Raised when the three-way merge inputs are structurally invalid."""
