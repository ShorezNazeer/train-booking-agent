"""Pull current 2026 NTES train schedules for major corridors, Vande Bharat,
Rajdhani, Shatabdi, and all NDLS-CNB trains, saving to raw_data/ntes_schedules.json.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ntes import NTESClient

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw_data"
OUT_FILE = RAW_DIR / "ntes_schedules.json"

CORRIDOR_PAIRS = [
    # Delhi - Kanpur corridor
    ("NDLS", "CNB"), ("CNB", "NDLS"),
    ("DLI", "CNB"), ("CNB", "DLI"),
    ("ANVT", "CNB"), ("CNB", "ANVT"),
    ("NZM", "CNB"), ("CNB", "NZM"),
    # Delhi - Lucknow corridor
    ("NDLS", "LKO"), ("LKO", "NDLS"),
    ("NDLS", "LJN"), ("LJN", "NDLS"),
    ("CNB", "LKO"), ("LKO", "CNB"),
    ("CNB", "LJN"), ("LJN", "CNB"),
    # Intermediate corridor
    ("NDLS", "ALJN"), ("ALJN", "NDLS"),
    ("ALJN", "CNB"), ("CNB", "ALJN"),
    # Delhi - Varanasi
    ("NDLS", "BSB"), ("BSB", "NDLS"),
    ("NDLS", "PRYJ"), ("PRYJ", "NDLS"),
    # Delhi - Howrah
    ("NDLS", "HWH"), ("HWH", "NDLS"),
    # Delhi - Mumbai
    ("NDLS", "MMCT"), ("MMCT", "NDLS"),
    ("NZM", "BDTS"), ("BDTS", "NZM"),
    ("NZM", "CSMT"), ("CSMT", "NZM"),
]

SPECIFIC_TRAINS = [
    "12004", "12003",  # Swarna Shatabdi
    "12302", "12301",  # Howrah Rajdhani
    "22436", "22435",  # Vande Bharat NDLS-BSB
    "22438", "22437",  # Prayagraj Humsafar
    "22439", "22440",  # Vande Bharat NDLS-SVDK
    "20802", "20801",  # Magadh Express
    "12452", "12451",  # Shram Shakti Express
    "12560", "12559",  # Shiv Ganga Express
    "12582", "12581",  # Banaras SF Express
    "12314", "12313",  # Sealdah Rajdhani
    "12310", "12309",  # Patna Rajdhani
    "12424", "12423",  # Dibrugarh Rajdhani
    "12952", "12951",  # Mumbai Rajdhani
    "12954", "12953",  # August Kranti Rajdhani
    "12002", "12001",  # Bhopal Shatabdi
    "12005", "12006",  # Kalka Shatabdi
    "12011", "12012",  # Kalka Shatabdi
    "12013", "12014",  # Amritsar Shatabdi
    "12015", "12016",  # Ajmer Shatabdi
    "12017", "12018",  # Dehradun Shatabdi
]

VB_PREFIXES = ["206", "207", "208", "209", "224", "225"]


def collect_train_numbers(client: NTESClient) -> dict[str, dict]:
    trains = {}

    print("Collecting corridor trains...")
    for src, dst in CORRIDOR_PAIRS:
        try:
            res = client.trains_between(src, dst)
            for t in res.get("Trains", []):
                num = str(t.get("TrainNumber", "")).strip()
                if num and num not in trains:
                    trains[num] = t
        except Exception as e:
            print(f"  Warning: failed {src}->{dst}: {e}")

    print(f"Collected {len(trains)} trains from corridors.")

    print("Searching for Vande Bharat trains...")
    for pfx in VB_PREFIXES:
        try:
            res = client.search(pfx)
            for t in res.get("Trains", []):
                name = t.get("TrainName", "").upper()
                ttype = t.get("Type", "")
                if "VANDE" in name or ttype == "VNDB":
                    num = str(t.get("TrainNumber", "")).strip()
                    if num and num not in trains:
                        trains[num] = t
        except Exception as e:
            print(f"  Warning: search {pfx} failed: {e}")

    for num in SPECIFIC_TRAINS:
        if num not in trains:
            trains[num] = {"TrainNumber": num}

    print(f"Total unique trains to fetch: {len(trains)}")
    return trains


def pull_schedules(client: NTESClient, train_numbers: list[str]) -> list[dict]:
    schedules = []
    failed = []

    def fetch(num):
        for attempt in range(3):
            try:
                s = client.schedule(num)
                if s and s.get("stations"):
                    return s
            except Exception:
                time.sleep(0.5)
        return None

    t0 = time.time()
    print(f"Pulling schedules for {len(train_numbers)} trains using ThreadPoolExecutor...")
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch, train_numbers))

    for num, sched in zip(train_numbers, results):
        if sched and sched.get("stations"):
            schedules.append(sched)
        else:
            failed.append(num)

    print(f"Successfully fetched {len(schedules)} schedules in {time.time()-t0:.2f}s (failed: {len(failed)}).")
    return schedules


def main():
    client = NTESClient()
    train_meta = collect_train_numbers(client)
    schedules = pull_schedules(client, list(train_meta.keys()))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(schedules)} schedules to {OUT_FILE}.")


if __name__ == "__main__":
    main()
