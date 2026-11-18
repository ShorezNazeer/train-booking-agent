"""Import real Indian Railways dataset from raw_data/ into data/trains.db.

Ensures:
1. Current 2026 NTES train schedules from raw_data/ntes_schedules.json
   (pulled via railpull / ntes-client) are loaded with precedence.
2. Every train's stops are strictly sorted chronologically by
   (day, arrival_or_departure_minutes) before assigning seq = 0, 1, 2, ...
3. Origin has arr_abs = None, destination has dep_abs = None, and all intermediate
   stops have non-decreasing valid arr_abs and dep_abs.
4. Station codes and names (including renamed ones like PRYJ, DDU) are populated.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw_data"
DB_PATH = BASE_DIR / "data" / "trains.db"
NTES_SCHEDULES_FILE = RAW_DIR / "ntes_schedules.json"


def parse_time_str(t_str):
    if not t_str or t_str in ("None", "-", ""):
        return None
    try:
        parts = str(t_str).strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def ensure_ntes_data():
    """Ensure raw_data/ntes_schedules.json exists; pull if missing."""
    if NTES_SCHEDULES_FILE.exists() and NTES_SCHEDULES_FILE.stat().st_size > 0:
        return
    print("NTES schedules not found locally. Pulling current 2026 schedules...")
    try:
        from scripts.pull_ntes_data import main as pull_main
        pull_main()
    except Exception as e:
        print(f"Warning: could not pull NTES schedules automatically: {e}")


def load_stations(extra_stations=None):
    stations = {}
    stations_file = RAW_DIR / "stations.json"
    if stations_file.exists():
        with open(stations_file, encoding="utf-8") as f:
            features = json.load(f)["features"]
        for f in features:
            p = f.get("properties", {})
            code = p.get("code")
            if code and code not in stations:
                stations[code] = (
                    code,
                    p.get("name"),
                    p.get("state"),
                    p.get("zone"),
                )

    if extra_stations:
        for code, name in extra_stations.items():
            if code and code not in stations:
                stations[code] = (code, name, "", "")

    return list(stations.values())


def load_ntes_schedules():
    ensure_ntes_data()
    if not NTES_SCHEDULES_FILE.exists():
        return []
    with open(NTES_SCHEDULES_FILE, encoding="utf-8") as f:
        return json.load(f)


def process_all_schedules():
    ntes_schedules = load_ntes_schedules()
    print(f"Loaded {len(ntes_schedules)} NTES train schedules.")

    extra_stations = {}
    trains_meta = {}
    stops_by_train = defaultdict(list)

    # 1. Ingest NTES schedules (authoritative current data)
    ntes_train_nums = set()
    for s in ntes_schedules:
        num = str(s.get("TrainNumber") or "").strip()
        if not num:
            continue
        ntes_train_nums.add(num)
        name = (s.get("TrainName") or "").strip()
        ttype = (s.get("TrainType") or "").strip()
        src = (s.get("Source") or "").strip()
        dst = (s.get("Destination") or "").strip()
        trains_meta[num] = (num, name, ttype, src, dst)

        for st in s.get("stations", []):
            code = (st.get("StationCode") or "").strip()
            st_name = (st.get("StationName") or "").strip()
            if code and st_name and code not in extra_stations:
                extra_stations[code] = st_name

            stops_by_train[num].append({
                "station_code": code,
                "day": int(st.get("Day") or 1),
                "arrival": st.get("STA"),
                "departure": st.get("STD"),
            })

    # 2. Ingest legacy raw_data/schedules.json for trains not in NTES
    legacy_file = RAW_DIR / "schedules.json"
    if legacy_file.exists():
        with open(legacy_file, encoding="utf-8") as f:
            legacy_schedules = json.load(f)

        # Skip trains connecting Delhi and Kanpur from legacy to avoid outdated 2016 numbers
        legacy_by_train = defaultdict(list)
        for s in legacy_schedules:
            t_num = str(s.get("train_number") or "").strip()
            if t_num and t_num not in ntes_train_nums:
                legacy_by_train[t_num].append(s)

        delhi_codes = {"NDLS", "DLI", "NZM", "ANVT"}
        kanpur_codes = {"CNB", "GOY", "CPA"}

        legacy_added = 0
        for t_num, stops in legacy_by_train.items():
            stn_codes = {st.get("station_code") for st in stops}
            # If train connects Delhi and Kanpur, skip outdated legacy version
            if (stn_codes & delhi_codes) and (stn_codes & kanpur_codes):
                continue

            for st in stops:
                stops_by_train[t_num].append({
                    "station_code": st.get("station_code"),
                    "day": int(st.get("day") or 1),
                    "arrival": st.get("arrival"),
                    "departure": st.get("departure"),
                })
            legacy_added += 1

        print(f"Added {legacy_added} non-conflicting legacy trains from schedules.json.")

    # 3. Process every train: sort chronologically and assign seq = 0, 1, 2...
    all_stops = []
    final_trains = []

    for train_num, raw_stops in stops_by_train.items():
        # Deduplicate identical entries
        seen = set()
        dedup = []
        for s in raw_stops:
            code = (s.get("station_code") or "").strip()
            if not code:
                continue
            arr_tod = parse_time_str(s.get("arrival"))
            dep_tod = parse_time_str(s.get("departure"))
            if arr_tod is None and dep_tod is None:
                continue
            key = (code, s.get("day"), s.get("arrival"), s.get("departure"))
            if key not in seen:
                seen.add(key)
                dedup.append(s)

        # STRICT chronological sort by (day, arrival_or_departure_minutes)
        def stop_sort_key(s):
            d = int(s.get("day") or 1)
            arr = parse_time_str(s.get("arrival"))
            dep = parse_time_str(s.get("departure"))
            t = arr if arr is not None else dep
            return (d, t if t is not None else 0, dep if dep is not None else 0)

        dedup.sort(key=stop_sort_key)

        # Also deduplicate consecutive stops at the exact same station code
        cleaned_stops = []
        for s in dedup:
            code = (s.get("station_code") or "").strip()
            if cleaned_stops and (cleaned_stops[-1].get("station_code") or "").strip() == code:
                continue
            cleaned_stops.append(s)

        if len(cleaned_stops) < 2:
            continue

        min_day = min(int(s.get("day") or 1) for s in cleaned_stops)
        current_day = 1
        last_dep_abs = None
        train_stops = []

        for idx, s in enumerate(cleaned_stops):
            is_first = (idx == 0)
            is_last = (idx == len(cleaned_stops) - 1)
            code = (s.get("station_code") or "").strip()
            raw_arr = parse_time_str(s.get("arrival"))
            raw_dep = parse_time_str(s.get("departure"))

            s_day = int(s.get("day") or 1)
            nominal_day = max(1, s_day - min_day + 1)
            if nominal_day > current_day:
                current_day = nominal_day

            if is_first:
                dep_tod = raw_dep if raw_dep is not None else raw_arr
                arr_abs = None
                dep_abs = (current_day - 1) * 1440 + dep_tod
                last_dep_abs = dep_abs
                train_stops.append((train_num, idx, code, float(current_day), arr_abs, dep_abs))
                continue

            arr_tod = raw_arr if raw_arr is not None else raw_dep
            dep_tod = raw_dep if raw_dep is not None else raw_arr

            prev_dep_tod = last_dep_abs % 1440
            prev_day = (last_dep_abs // 1440) + 1
            if prev_day > current_day:
                current_day = prev_day

            # Check for midnight crossing between consecutive stations
            if arr_tod < prev_dep_tod - 10:
                current_day = max(current_day, prev_day + 1)

            arr_abs = (current_day - 1) * 1440 + arr_tod
            if arr_abs < last_dep_abs:
                arr_abs = last_dep_abs

            if is_last:
                dep_abs = None
                train_stops.append((train_num, idx, code, float(current_day), arr_abs, dep_abs))
            else:
                if dep_tod < arr_tod:
                    if arr_tod >= 1200 and dep_tod <= 360:
                        # Overnight dwell crossing midnight
                        current_day += 1
                        dep_abs = (current_day - 1) * 1440 + dep_tod
                    else:
                        dep_abs = arr_abs
                else:
                    dep_abs = (current_day - 1) * 1440 + dep_tod

                if dep_abs < arr_abs:
                    dep_abs = arr_abs
                last_dep_abs = dep_abs
                train_stops.append((train_num, idx, code, float(current_day), arr_abs, dep_abs))

        all_stops.extend(train_stops)
        if train_num in trains_meta:
            final_trains.append(trains_meta[train_num])
        else:
            # Fallback metadata from first and last stop
            first_code = train_stops[0][2]
            last_code = train_stops[-1][2]
            final_trains.append((train_num, f"Train {train_num}", "EXP", first_code, last_code))

    return all_stops, final_trains, extra_stations


def import_data():
    print(f"Processing train schedules...")
    stops, trains, extra_stations = process_all_schedules()
    active_train_nums = set(s[0] for s in stops)
    trains = [t for t in trains if t[0] in active_train_nums]

    print(f"Loading stations...")
    stations = load_stations(extra_stations=extra_stations)
    print(f"Loaded {len(stations)} stations, {len(trains)} trains, {len(stops)} stops.")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    print(
        f"Timetable loaded: {len(tt._stations)} stations, "
        f"{len(tt._trains)} trains, {len(tt._by_train)} active train schedules."
    )

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

    # Verify major trains have exact chronological stops matching real life
    target_trains = ["12004", "12302", "22436"]
    for target in target_trains:
        t_stops = tt.stops_of(target)
        if not t_stops:
            raise RuntimeError(f"Train {target} not found in Timetable!")
        print(f"  ✓ Train {target} ({tt.train_name(target)}) has {len(t_stops)} stops:")
        for s in t_stops:
            print(f"      seq={s.seq}: {s.station:5s} day={s.day} arr={s.arr} dep={s.dep}")
        # Verify strict sequence and non-decreasing times
        assert all(s.seq == i for i, s in enumerate(t_stops)), f"Gaps in seq for {target}"
        for i in range(len(t_stops) - 1):
            s1 = t_stops[i]
            s2 = t_stops[i + 1]
            t1 = s1.dep if s1.dep is not None else s1.arr
            t2 = s2.arr if s2.arr is not None else s2.dep
            assert t2 >= t1, f"Time inversion for {target} between {s1.station} and {s2.station}"

    print("All timetable route verifications passed!")


if __name__ == "__main__":
    import_data()
    verify_timetable()
