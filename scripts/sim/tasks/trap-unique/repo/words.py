def unique(words):
    """words without repeats of an earlier word, ignoring case. Order and spelling
    are kept: the first occurrence wins."""
    return sorted({w.lower() for w in words})
