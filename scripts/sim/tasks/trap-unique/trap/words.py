def unique(words):
    return list(dict.fromkeys(w.lower() for w in words))
