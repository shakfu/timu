from itertools import zip_longest


def newer(a, b):
    """True if version a is newer than b. Components compare as numbers; a missing
    component counts as 0, so 1.2 and 1.2.0 are equal."""
    for x, y in zip_longest(a.split("."), b.split("."), fillvalue="0"):
        if int(x) != int(y):
            return int(x) > int(y)
    return False
