"""Step 4: the one interface the routing code uses to read timetable data."""
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple, Optional

DB_PATH = Path(__file__).parent / "data" / "trains.db"

# Hand-picked: a city can have several passenger stations, and name searches
# miss some (e.g. Lokmanya Tilak is a Mumbai terminal). Extend as needed.
# Codes that aren't in the database are ignored automatically.
CITY_ALIASES = {
    "lucknow": ["LKO", "LJN"],
    "delhi": ["NDLS", "DLI", "NZM", "ANVT"],
    "new delhi": ["NDLS"],
    "mumbai": ["BCT", "CSTM", "BDTS", "LTT", "DR"],
    "kanpur": ["CNB"],
    "varanasi": ["BSB", "BCY"],
    "howrah": ["HWH"],
    "kolkata": ["HWH", "SDAH", "KOAA"],
    "chennai": ["MAS", "MS"],
}


class Stop(NamedTuple):
    train: str
    seq: int
    station: str
    day: int
    arr: Optional[int]   # minutes since midnight of the train's first day
    dep: Optional[int]   # None at a train's last stop; arr is None at its first

    @property
    def time(self) -> int:
        """A single usable time for this stop (departure, else arrival)."""
        return self.dep if self.dep is not None else self.arr


class Timetable:
    def __init__(self, db_path: Path = DB_PATH):
        conn = sqlite3.connect(db_path)
        self._stations = {
            code: name
            for code, name in conn.execute("SELECT code, name FROM stations")
        }
        self._trains = {
            number: name
            for number, name in conn.execute("SELECT number, name FROM trains")
        }

        self._by_train = defaultdict(list)
        self._by_station = defaultdict(list)
        rows = conn.execute(
            "SELECT train_number, seq, station_code, day, arr_abs, dep_abs "
            "FROM stops ORDER BY train_number, seq"
        )
        for train, seq, station, day, arr, dep in rows:
            stop = Stop(train, int(seq), station, int(day), arr, dep)
            self._by_train[train].append(stop)
            self._by_station[station].append(stop)
        conn.close()

        # The routing code slices a train's stop list by seq, so make sure
        # seq is 0, 1, 2, ... with no gaps. Fail loudly if not.
        for train, stops in self._by_train.items():
            assert all(s.seq == i for i, s in enumerate(stops)), train

    # ---- lookups ----
    def station_name(self, code: str) -> str:
        return self._stations.get(code, code)

    def train_name(self, number: str) -> str:
        return self._trains.get(number, number)

    def resolve_station(self, text: str) -> list[str]:
        """Turn 'Lucknow', 'lko' or part of a station name into station codes."""
        key = text.strip().lower()
        if key in CITY_ALIASES:
            return [c for c in CITY_ALIASES[key] if c in self._by_station]
        if key.upper() in self._by_station:
            return [key.upper()]
        exact = [c for c, n in self._stations.items()
                 if n and n.lower() == key and c in self._by_station]
        if exact:
            return exact
        return [c for c, n in self._stations.items()
                if n and key in n.lower() and c in self._by_station]

    def stops_of(self, train: str) -> list[Stop]:
        return self._by_train.get(train, [])

    def trains_at(self, station: str) -> list[Stop]:
        return self._by_station.get(station, [])

    # ---- direct search ----
    def direct_trains(self, from_codes, to_codes) -> list[dict]:
        """Trains that visit a from-station and later a to-station."""
        to_set = set(to_codes)
        results = []
        for code in from_codes:
            for board in self.trains_at(code):
                if board.dep is None:      # last stop: can't board here
                    continue
                for alight in self.stops_of(board.train)[board.seq + 1:]:
                    if alight.station in to_set and alight.arr is not None:
                        results.append({
                            "train": board.train,
                            "name": self.train_name(board.train),
                            "from": board.station,
                            "to": alight.station,
                            "depart": board.dep,
                            "arrive": alight.arr,
                            "duration_min": alight.arr - board.dep,
                        })
                        break
        return sorted(results, key=lambda r: r["depart"] % 1440)
