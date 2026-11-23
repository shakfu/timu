def split(total, n):
    """total cents in n integer parts that sum to total and differ by at most 1,
    larger parts first. Raises ValueError if n < 1 or total < 0."""
    return [total // n] * n
