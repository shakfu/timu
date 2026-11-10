import re

PATTERN = re.compile(r"(?:([0-9]{1,4})h)?(?:([0-9]{1,4})m)?(?:([0-9]{1,4})s)?")


def parse_duration(s):
    m = PATTERN.fullmatch(s)
    if not s or m is None:
        raise ValueError(f"not a duration: {s!r}")
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + sec
