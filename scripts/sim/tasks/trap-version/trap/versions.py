def newer(a, b):
    return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))
