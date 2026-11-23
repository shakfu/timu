def split(total, n):
    q, r = divmod(total, n)
    return [q + r] + [q] * (n - 1)
