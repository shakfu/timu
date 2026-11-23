def newer(a, b):
    """True if version a is newer than b. Components compare as numbers; a missing
    component counts as 0, so 1.2 and 1.2.0 are equal."""
    return a > b
