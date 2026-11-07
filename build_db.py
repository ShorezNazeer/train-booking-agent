"""Step 3: clean the raw datameet/railways JSON and build data/trains.db."""
import json
import sqlite3
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw" / "railways"
DB_PATH = HERE / "data" / "trains.db"

# Subset settings: keep trains that stop at several of the busiest stations.
TOP_N_STATIONS = 40
MIN_MAJOR_STOPS = 3


def load_schedules() -> pd.DataFrame:
    with open(RAW / "schedules.json") as f:
        df = pd.DataFrame(json.load(f))

    df = df.drop_duplicates(
        subset=["train_number", "station_code", "day", "arrival", "departure"]
    )
    # Some rows have no day number; they can't be placed in time, so drop them.
    df = df.dropna(subset=["day"])


    # The dataset uses the text 'None' for missing times; convert to real nulls
    # and then to minutes since midnight.
    for col in ("arrival", "departure"):
        df[col] = df[col].replace("None", pd.NA)
        df[f"{col}_min"] = (
            pd.to_timedelta(df[col], errors="coerce").dt.total_seconds() / 60
        )

    # 'day' is 1 for the first day of the journey, 2 after midnight, etc.
    # Convert to absolute minutes since midnight of day 1.
    offset = (df["day"] - 1) * 1440
    df["arr_abs"] = df["arrival_min"] + offset
    df["dep_abs"] = df["departure_min"] + offset

    # If the departure is earlier than the arrival in the same row, the train
    # waited through midnight at that station.
    crossed = df["dep_abs"] < df["arr_abs"]
    df.loc[crossed, "dep_abs"] += 1440
    # Drop stops that have no usable time at all.
    df = df.dropna(subset=["arr_abs", "dep_abs"], how="all")
    # The data has no stop-order column, so rebuild it from the times.
    df["sort_key"] = df["arr_abs"].fillna(df["dep_abs"])
    df = df.sort_values(["train_number", "sort_key"])
    df["seq"] = df.groupby("train_number").cumcount()
    return df


def pick_subset(sched: pd.DataFrame) -> pd.DataFrame:
    counts = sched.groupby("station_code")["train_number"].nunique()
    major = set(counts.sort_values(ascending=False).head(TOP_N_STATIONS).index)

    hits = (
        sched[sched["station_code"].isin(major)]
        .groupby("train_number")["station_code"]
        .nunique()
    )
    keep = hits[hits >= MIN_MAJOR_STOPS].index
    return sched[sched["train_number"].isin(keep)]


def load_trains(keep_numbers) -> pd.DataFrame:
    with open(RAW / "trains.json") as f:
        feats = json.load(f)["features"]
    rows = [
        {
            "number": str(p["number"]),
            "name": p["name"],
            "type": p.get("type"),
            "from_code": p.get("from_station_code"),
            "to_code": p.get("to_station_code"),
        }
        for p in (ft["properties"] for ft in feats)
    ]
    df = pd.DataFrame(rows)
    return df[df["number"].isin(set(keep_numbers))].drop_duplicates("number")


def load_stations(keep_codes) -> pd.DataFrame:
    with open(RAW / "stations.json") as f:
        feats = json.load(f)["features"]
    rows = [
        {
            "code": p["code"],
            "name": p.get("name"),
            "state": p.get("state"),
            "zone": p.get("zone"),
        }
        for p in (ft["properties"] for ft in feats)
    ]
    df = pd.DataFrame(rows)
    return df[df["code"].isin(set(keep_codes))].drop_duplicates("code")


def main():
    sched = pick_subset(load_schedules())
    trains = load_trains(sched["train_number"].unique())
    stations = load_stations(sched["station_code"].unique())

    stops = sched[["train_number", "seq", "station_code", "day", "arr_abs", "dep_abs"]].copy()
    for col in ("arr_abs", "dep_abs"):
        stops[col] = stops[col].round().astype("Int64")

    DB_PATH.parent.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    with sqlite3.connect(DB_PATH) as conn:
        stations.to_sql("stations", conn, index=False)
        trains.to_sql("trains", conn, index=False)
        stops.to_sql("stops", conn, index=False)
        conn.execute("CREATE INDEX idx_stops_station ON stops(station_code)")
        conn.execute("CREATE INDEX idx_stops_train ON stops(train_number, seq)")

    print(f"Built {DB_PATH}")
    print(f"  stations: {len(stations)}")
    print(f"  trains:   {len(trains)}")
    print(f"  stops:    {len(stops)}")


if __name__ == "__main__":
    main()
