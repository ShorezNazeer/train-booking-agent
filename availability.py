"""Step 5: simulated seat availability (the timetable data has no live seats)."""
import hashlib
from datetime import date

CLASSES = ("SL", "3A", "2A")
CAPACITY = {"SL": 72, "3A": 64, "2A": 48}
SOLD_OUT_CHANCE = 0.25  # chance that a train/date/class has no seats

# For tests and demos: force trains sold out.
# Entries are (train_number, "YYYY-MM-DD"), or (train_number, None) for every date.
FORCED_SOLD_OUT: set = set()


def _unit(train: str, travel_date: date, cls: str) -> float:
    """A stable pseudo-random number in [0, 1] for this train/date/class."""
    key = f"{train}|{travel_date.isoformat()}|{cls}".encode()
    return int(hashlib.sha256(key).hexdigest()[:8], 16) / 0xFFFFFFFF


def seats_available(train: str, travel_date: date, cls: str) -> int:
    """Seats left in a class. travel_date is the date you board the train."""
    if (train, travel_date.isoformat()) in FORCED_SOLD_OUT or (train, None) in FORCED_SOLD_OUT:
        return 0
    u = _unit(train, travel_date, cls)
    if u < SOLD_OUT_CHANCE:
        return 0
    scaled = (u - SOLD_OUT_CHANCE) / (1 - SOLD_OUT_CHANCE)
    return 1 + int(scaled * (CAPACITY[cls] - 1))


def availability(train: str, travel_date: date) -> dict:
    """Seats per class that still have any, e.g. {'SL': 31, '2A': 7}."""
    seats = {c: seats_available(train, travel_date, c) for c in CLASSES}
    return {c: n for c, n in seats.items() if n > 0}


def has_seats(train: str, travel_date: date) -> bool:
    return bool(availability(train, travel_date))
