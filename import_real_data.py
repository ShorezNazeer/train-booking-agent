"""Import real Indian Railways dataset from raw_data/ into data/trains.db."""
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw_data"
DB_PATH = BASE_DIR / "data" / "trains.db"
BACKUP_DB_PATH = BASE_DIR / "data" / "trains_backup.db"


def parse_time_str(t_str):
    if not t_str or t_str == "None":
        return None
    try:
        parts = t_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def load_stations():
    with open(RAW_DIR / "stations.json") as f:
        features = json.load(f)["features"]
    seen = set()
    rows = []
    for f in features:
        p = f.get("properties", {})
        code = p.get("code")
        if code and code not in seen:
            seen.add(code)
            rows.append((
                code,
                p.get("name"),
                p.get("state"),
                p.get("zone"),
            ))
    return rows


def load_trains(active_train_numbers):
    with open(RAW_DIR / "trains.json") as f:
        features = json.load(f)["features"]
    seen = set()
    rows = []
    for f in features:
        p = f.get("properties", {})
        num = str(p.get("number"))
        if num in active_train_numbers and num not in seen:
            seen.add(num)
            rows.append((
                num,
                p.get("name"),
                p.get("type"),
                p.get("from_station_code"),
                p.get("to_station_code"),
            ))
    return rows


def process_schedules():
    with open(RAW_DIR / "schedules.json") as f:
        schedules = json.load(f)

    by_train = defaultdict(list)
    for s in schedules:
        by_train[str(s["train_number"])].append(s)

    all_stops = []

    for train_num, stops in by_train.items():
        # Deduplicate identical entries
        seen = set()
        dedup = []
        for s in stops:
            key = (s.get("station_code"), s.get("day"), s.get("arrival"), s.get("departure"))
            if key not in seen:
                seen.add(key)
                dedup.append(s)

        # Sort stops in natural route order (by schedule ID)
        dedup.sort(key=lambda x: x["id"])

        # Keep stops that have at least arrival or departure time
        valid = [
            s for s in dedup
            if (s.get("arrival") and s.get("arrival") != "None")
            or (s.get("departure") and s.get("departure") != "None")
        ]
        if len(valid) < 2:
            continue

        # Determine origin nominal day
        min_day = None
        for s in valid:
            if s.get("day") is not None:
                if min_day is None or s["day"] < min_day:
                    min_day = s["day"]
        if min_day is None:
            min_day = 1

        current_day = 1
        last_dep_abs = None
        train_stops = []

        for idx, s in enumerate(valid):
            is_first = (idx == 0)
            is_last = (idx == len(valid) - 1)

            raw_arr = parse_time_str(s.get("arrival"))
            raw_dep = parse_time_str(s.get("departure"))

            s_day = s.get("day")
            if s_day is not None:
                nominal_day = max(1, int(s_day - min_day + 1))
                if nominal_day > current_day:
                    current_day = nominal_day

            if is_first:
                dep_tod = raw_dep if raw_dep is not None else raw_arr
                arr_abs = None
                dep_abs = (current_day - 1) * 1440 + dep_tod
                last_dep_abs = dep_abs
                train_stops.append((train_num, idx, s["station_code"], float(current_day), arr_abs, dep_abs))
                continue

            arr_tod = raw_arr if raw_arr is not None else raw_dep
            dep_tod = raw_dep if raw_dep is not None else raw_arr

            prev_dep_tod = last_dep_abs % 1440
            prev_day = (last_dep_abs // 1440) + 1
            if prev_day > current_day:
                current_day = prev_day

            # Check for midnight crossing between stations
            if arr_tod < prev_dep_tod - 10:
                current_day = max(current_day, prev_day + 1)

            arr_abs = (current_day - 1) * 1440 + arr_tod
            if arr_abs < last_dep_abs:
                arr_abs = last_dep_abs

            if is_last:
                dep_abs = None
                train_stops.append((train_num, idx, s["station_code"], float(current_day), arr_abs, dep_abs))
            else:
                # Handle dwell time at intermediate station
                if dep_tod < arr_tod:
                    if arr_tod >= 1200 and dep_tod <= 360:
                        # Overnight dwell crossing midnight
                        current_day += 1
                        dep_abs = (current_day - 1) * 1440 + dep_tod
                    else:
                        # Dwell recording typo (e.g. 11:03 arr, 11:02 dep)
                        dep_abs = arr_abs
                else:
                    dep_abs = (current_day - 1) * 1440 + dep_tod

                if dep_abs < arr_abs:
                    dep_abs = arr_abs
                last_dep_abs = dep_abs
                train_stops.append((train_num, idx, s["station_code"], float(current_day), arr_abs, dep_abs))

        all_stops.extend(train_stops)

    return all_stops


def import_data():
    print(f"Reading raw data from {RAW_DIR}...")
    stations = load_stations()
    print(f"Loaded {len(stations)} stations.")

    stops = process_schedules()
    active_train_nums = set(s[0] for s in stops)
    print(f"Processed {len(stops)} stops across {len(active_train_nums)} trains.")

    trains = load_trains(active_train_nums)
    print(f"Loaded {len(trains)} trains.")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Temporary database to build atomically
    temp_db = DB_PATH.with_suffix(".tmp")
    if temp_db.exists():
        temp_db.unlink()

    print(f"Writing to SQLite database {DB_PATH}...")
    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE stations (
            code TEXT,
            name TEXT,
            state TEXT,
            zone TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE trains (
            number TEXT,
            name TEXT,
            type TEXT,
            from_code TEXT,
            to_code TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE stops (
            train_number TEXT,
            seq INTEGER,
            station_code TEXT,
            day REAL,
            arr_abs INTEGER,
            dep_abs INTEGER
        )
    """)

    cur.executemany("INSERT INTO stations VALUES (?,?,?,?)", stations)
    cur.executemany("INSERT INTO trains VALUES (?,?,?,?,?)", trains)
    cur.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?)", stops)

    cur.execute("CREATE INDEX idx_stops_station ON stops(station_code)")
    cur.execute("CREATE INDEX idx_stops_train ON stops(train_number, seq)")

    conn.commit()
    conn.close()

    temp_db.replace(DB_PATH)
    print(f"Successfully populated {DB_PATH}.")


def verify_timetable():
    print("Verifying Timetable() loads cleanly and finds routes...")
    from data_source import Timetable
    from routing import plan_trip

    tt = Timetable(DB_PATH)
    print(f"Timetable loaded: {len(tt._stations)} stations, {len(tt._trains)} trains, {len(tt._by_train)} active train schedules.")

    pairs = [
        ("NDLS", "CNB"),
        ("NDLS", "ALJN"),
        ("ALJN", "CNB"),
        ("CNB", "LKO"),
        ("NDLS", "LKO"),
    ]
    travel_date = date.today()
    for src, dst in pairs:
        routes = plan_trip(tt, src, dst, travel_date)
        if not routes:
            raise RuntimeError(f"No routes found between {src} and {dst}!")
        print(f"  ✓ {src} -> {dst}: {len(routes)} route(s) found. Earliest duration: {routes[0].duration} min")

    print("All timetable route verifications passed!")


if __name__ == "__main__":
    import_data()
    verify_timetable()
