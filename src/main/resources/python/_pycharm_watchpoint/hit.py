"""The :class:`WatchpointHit` exception (leaf module, no runtime deps)."""


# ---------------------------------------------------------------------------
# WatchpointHit exception
# ---------------------------------------------------------------------------

class WatchpointHit(Exception):
    """Raised when a watched variable or attribute changes value.

    Attributes:
        watch_name:  The expression string passed to watch() (e.g. 'x' or 'obj.val').
        old_value:   repr() of the value before the change.
        new_value:   repr() of the value after the change.
        source_file: Absolute path of the file where the change occurred.
        source_line: Line number of the statement that performed the change.
    """

    def __init__(self, watch_name: str, old_value: str, new_value: str,
                 source_file: str, source_line: int) -> None:
        self.watch_name = watch_name
        self.old_value = old_value
        self.new_value = new_value
        self.source_file = source_file
        self.source_line = source_line
        super().__init__(
            f"Watchpoint: '{watch_name}' changed from {old_value} to {new_value} "
            f"at {source_file}:{source_line}"
        )
