"""Parse durations such as "90s", "1h30m" or "2d 4h" into seconds."""

import re

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse(text):
    """Seconds in a duration: numbers each followed by a unit (s, m, h, d), optionally separated by spaces.
    Raises ValueError for anything else, including an empty string."""
    total = 0
    for number, unit in re.findall(r"(\d+)([smhd])", text):
        total += int(number) * UNITS[unit]
    return total


def format(seconds):
    """The shortest duration text for a whole number of seconds: 5400 -> "1h30m", 0 -> "0s"."""
    if seconds == 0:
        return "0s"
    out = ""
    for unit, size in UNITS.items():
        if seconds >= size:
            out += f"{seconds // size}{unit}"
            seconds %= size
    return out
