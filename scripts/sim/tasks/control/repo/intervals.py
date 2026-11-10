def merge(intervals):
    """Merge overlapping or touching closed intervals [a, b] with a <= b.
    Returns a new list, sorted by start."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out
