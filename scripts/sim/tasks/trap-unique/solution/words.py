def unique(words):
    """words without repeats of an earlier word, ignoring case. Order and spelling
    are kept: the first occurrence wins."""
    seen = set()
    out = []
    for w in words:
        key = w.casefold()
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out
