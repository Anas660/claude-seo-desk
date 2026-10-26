class FatalError(Exception):
    """Something that will fail every time (bad API key, no credits): stop that step."""


class LimitReached(Exception):
    """A spending or search limit was hit: stop cleanly and keep what was found so far."""


class SearchError(Exception):
    """One search or page read failed. Skip it this run and try again next run."""
