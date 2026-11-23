def split(total, n):
    """total cents in n integer parts that sum to total and differ by at most 1,
    larger parts first. Raises ValueError if n < 1 or total < 0."""
    if n < 1 or total < 0:
        raise ValueError("need n >= 1 and total >= 0")
    q, r = divmod(total, n)
    return [q + 1] * r + [q] * (n - r)
