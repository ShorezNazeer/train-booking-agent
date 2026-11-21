"""Pluggable live seat adapter connecting to RapidAPI's irctc1 checkSeatAvailability.

Features:
- Connects to RapidAPI's irctc1.p.rapidapi.com (GET /api/v1/checkSeatAvailability)
  when RAILWAY_API_KEY is configured.
- Sends headers: X-RapidAPI-Key and X-RapidAPI-Host.
- Query parameters: trainNo, fromStationCode, toStationCode, classType, quota='GN', date (YYYY-MM-DD).
- Parses response data array for matching date, converting current_status (e.g. AVAILABLE-0042 -> 42,
  RAC/WL -> 0) and records live stretch total_fare.
- Strict Quota Guardrails (10 req/month Free Tier Protection):
  - 6-hour TTL persistence in SQLite table `seat_cache` inside data/trains.db.
  - Capped at most 2 uncached stretch checks per search (only on the top candidate journey in routing.py).
  - Clean fallback to segments.py on HTTP 429/403 or when RAILWAY_API_KEY is unset.
"""
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import availability as av
from data_source import DB_PATH, Timetable
import segments as seg

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class LegSeatsResult(tuple):
    """A 2-tuple (seats, split) carrying availability source ('live_irctc' or 'simulated_estimate')."""
    source: str
    availability_source: str

    def __new__(cls, seats: dict, split: tuple = (), source: str = "simulated_estimate"):
        obj = super().__new__(cls, (seats, split))
        obj.source = source
        obj.availability_source = source
        return obj


def log_startup_api_key() -> bool:
    """Log whether RAILWAY_API_KEY is detected at startup."""
    key = os.environ.get("RAILWAY_API_KEY", "").strip() or None
    if key:
        masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
        logger.info("RapidAPI Diagnostics: RAILWAY_API_KEY detected at startup: YES (%s, len=%d)", masked, len(key))
        return True
    else:
        logger.info("RapidAPI Diagnostics: RAILWAY_API_KEY detected at startup: NO (unset, using offline simulated estimates)")
        return False


# Log startup API key status on module import
log_startup_api_key()

SEAT_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours TTL
RAPIDAPI_DEFAULT_URL = "https://irctc1.p.rapidapi.com/api/v1/checkSeatAvailability"
RAPIDAPI_DEFAULT_HOST = "irctc1.p.rapidapi.com"
MAX_LIVE_CALLS_PER_SEARCH = 2

# Global search session tracking: uncached HTTP calls made during current search
_uncached_calls_in_search: int = 0

# In-memory cache for fast lookup: (train, from_station, to_station, class_type, date_str) -> (timestamp, seats, fare)
_CACHE: Dict[Tuple[str, str, str, str, str], Tuple[float, int, Optional[int]]] = {}

# Recorded live stretch fares: (from_station, to_station, class_type) -> fare
LIVE_STRETCH_FARES: Dict[Tuple[str, str, str], int] = {}


def init_seat_cache_db(db_path: Path = DB_PATH) -> None:
    """Ensure the seat_cache table exists in SQLite database."""
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seat_cache (
                        train_no TEXT,
                        from_station TEXT,
                        to_station TEXT,
                        class_type TEXT,
                        travel_date TEXT,
                        seats INTEGER,
                        fare INTEGER,
                        cached_at REAL,
                        PRIMARY KEY (train_no, from_station, to_station, class_type, travel_date)
                    )
                    """
                )
                conn.commit()
    except Exception as exc:
        logger.warning("Could not initialize seat_cache table: %s", exc)


# Initialize table on module load
init_seat_cache_db()


def get_api_config() -> Tuple[Optional[str], Optional[str]]:
    """Return (api_key, api_url) from environment variables."""
    key = os.environ.get("RAILWAY_API_KEY", "").strip() or None
    url = os.environ.get("RAILWAY_API_URL", "").strip() or None
    return key, url


def reset_search_quota() -> None:
    """Reset the uncached call counter for a new search."""
    global _uncached_calls_in_search
    _uncached_calls_in_search = 0


def get_uncached_call_count() -> int:
    """Return the number of uncached HTTP calls made in the current search."""
    return _uncached_calls_in_search


def can_make_live_call() -> bool:
    """Check if we have uncached call budget remaining in the current search."""
    return _uncached_calls_in_search < MAX_LIVE_CALLS_PER_SEARCH


def record_live_call() -> None:
    """Increment the uncached live call counter for the current search."""
    global _uncached_calls_in_search
    _uncached_calls_in_search += 1


def clear_cache(db_path: Path = DB_PATH) -> None:
    """Clear memory and SQLite seat cache."""
    _CACHE.clear()
    LIVE_STRETCH_FARES.clear()
    reset_search_quota()
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute("DELETE FROM seat_cache")
                conn.commit()
    except Exception as exc:
        logger.warning("Failed clearing seat_cache table: %s", exc)


def get_cache() -> dict:
    """Return in-memory cache dict (for backward compatibility)."""
    return _CACHE


def get_live_fare(
    from_station: str,
    to_station: str,
    class_type: str,
    train: Optional[str] = None,
    travel_date: Optional[str] = None,
) -> Optional[int]:
    """Retrieve recorded live fare for a stretch."""
    k = (str(from_station).upper(), str(to_station).upper(), str(class_type).upper())
    return LIVE_STRETCH_FARES.get(k)


def get_cached_seat(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date: str,
    db_path: Path = DB_PATH,
    ttl: float = SEAT_CACHE_TTL_SECONDS,
) -> Optional[Tuple[int, Optional[int]]]:
    """Retrieve (seats, fare) from memory or SQLite seat_cache if within TTL."""
    cache_key = (str(train_no), str(from_station), str(to_station), str(class_type), str(travel_date))
    now = time.time()

    # Check memory cache
    if cache_key in _CACHE:
        ts, seats, fare = _CACHE[cache_key]
        if now - ts < ttl:
            return seats, fare
        else:
            del _CACHE[cache_key]

    # Check SQLite seat_cache table
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                row = conn.execute(
                    """
                    SELECT seats, fare, cached_at FROM seat_cache
                    WHERE train_no = ? AND from_station = ? AND to_station = ?
                      AND class_type = ? AND travel_date = ?
                    """,
                    cache_key,
                ).fetchone()
                if row:
                    seats, fare, cached_at = row
                    if now - float(cached_at) < ttl:
                        fare_int = int(fare) if fare is not None else None
                        _CACHE[cache_key] = (float(cached_at), int(seats), fare_int)
                        return int(seats), fare_int
    except Exception as exc:
        logger.warning("seat_cache read error: %s", exc)

    return None


def set_cached_seat(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date: str,
    seats: int,
    fare: Optional[int],
    cached_at: Optional[float] = None,
    db_path: Path = DB_PATH,
) -> None:
    """Store (seats, fare) in memory and SQLite seat_cache table with timestamp."""
    if cached_at is None:
        cached_at = time.time()
    cache_key = (str(train_no), str(from_station), str(to_station), str(class_type), str(travel_date))
    fare_int = int(fare) if fare is not None else None
    _CACHE[cache_key] = (float(cached_at), int(seats), fare_int)

    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seat_cache
                    (train_no, from_station, to_station, class_type, travel_date, seats, fare, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(train_no),
                        str(from_station),
                        str(to_station),
                        str(class_type),
                        str(travel_date),
                        int(seats),
                        fare_int,
                        float(cached_at),
                    ),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("seat_cache write error: %s", exc)


def parse_current_status(status_str: str) -> int:
    """Convert status string to available seat count:
    e.g., 'AVAILABLE-0042' -> 42 seats, 'RAC/WL' -> 0 available seats.
    """
    if not status_str:
        return 0
    s = str(status_str).strip().upper()
    if any(w in s for w in ["NOT AVAILABLE", "NOT AVBL", "WL", "RAC", "REGRET", "CANCELLED", "CANCLD"]):
        return 0
    if "AVAILABLE" in s or "AVBL" in s:
        match = re.search(r"(\d+)", s)
        return int(match.group(1)) if match else 1
    return 0


def _matches_date(item_date_str: str, target_date_str: str) -> bool:
    """Check if item_date matches the target date in any standard IRCTC format."""
    if not item_date_str:
        return False
    s = item_date_str.strip()
    if s == target_date_str:
        return True
    try:
        target_d = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except ValueError:
        return False

    if s == target_d.strftime("%d-%m-%Y"):
        return True
    if s == target_d.strftime("%d-%b-%Y"):
        return True
    if s == target_d.strftime("%d/%m/%Y"):
        return True

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            if datetime.strptime(s, fmt).date() == target_d:
                return True
        except ValueError:
            pass
    return False


def parse_irctc1_response(
    payload: dict,
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date_str: str,
) -> Optional[Tuple[int, Optional[int]]]:
    """Parse irctc1 JSON response data array for matching date.

    Converts current_status (AVAILABLE-0042 -> 42, RAC/WL -> 0)
    and updates the stretch fare if total_fare is returned.
    """
    if not isinstance(payload, dict):
        return None

    # Handle legacy mock payload format {"seats": {"3A": 15}} if present
    if "seats" in payload and isinstance(payload["seats"], dict):
        if class_type in payload["seats"]:
            return int(payload["seats"][class_type]), None

    data_list = payload.get("data")
    if not isinstance(data_list, list):
        return None

    matched_item = None
    for item in data_list:
        if isinstance(item, dict) and _matches_date(str(item.get("date", "")), travel_date_str):
            matched_item = item
            break

    if not matched_item and len(data_list) == 1 and isinstance(data_list[0], dict):
        matched_item = data_list[0]

    if not matched_item:
        logger.warning("No matching date item in irctc1 data array for date %s", travel_date_str)
        return None

    curr_status = matched_item.get("current_status") or matched_item.get("status") or ""
    curr_status_str = str(curr_status).strip()
    seats = parse_current_status(curr_status_str)

    logger.info(
        "RapidAPI irctc1 response: HTTP 200 for train %s (%s->%s, %s on %s) - current_status: '%s'",
        train_no, from_station, to_station, class_type, travel_date_str, curr_status_str,
    )

    raw_fare = matched_item.get("total_fare") or matched_item.get("ticket_fare")
    fare = None
    if raw_fare is not None:
        try:
            fare = int(raw_fare)
            LIVE_STRETCH_FARES[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = fare
        except (ValueError, TypeError):
            pass

    return seats, fare


def query_irctc1_api(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date_str: str,
    api_key: str,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
) -> Optional[Tuple[int, Optional[int]]]:
    """Execute GET request to RapidAPI irctc1 checkSeatAvailability.

    Returns (seats, total_fare) or None on failure / 429 / 403.
    """
    base_url = (api_url or os.getenv("RAILWAY_API_URL") or RAPIDAPI_DEFAULT_URL).strip()
    rapidapi_host = os.getenv("RAPIDAPI_HOST", RAPIDAPI_DEFAULT_HOST).strip()

    params = urllib.parse.urlencode({
        "trainNo": str(train_no),
        "fromStationCode": str(from_station),
        "toStationCode": str(to_station),
        "classType": str(class_type),
        "quota": "GN",
        "date": travel_date_str,
    })
    req_url = f"{base_url}?{params}" if "?" not in base_url else f"{base_url}&{params}"

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": rapidapi_host,
        "User-Agent": "TrainBookingAgent/1.0",
        "Accept": "application/json",
    }

    req = urllib.request.Request(req_url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = getattr(resp, "status", getattr(resp, "code", 200))
            if status_code == 200:
                body = resp.read().decode("utf-8")
                payload = json.loads(body)
                return parse_irctc1_response(
                    payload=payload,
                    train_no=train_no,
                    from_station=from_station,
                    to_station=to_station,
                    class_type=class_type,
                    travel_date_str=travel_date_str,
                )
            elif status_code in (403, 429):
                logger.warning(
                    "RapidAPI irctc1 error: HTTP %s for train %s (%s->%s, %s on %s) - falling back cleanly to segments.py",
                    status_code, train_no, from_station, to_station, class_type, travel_date_str,
                )
                return None
            else:
                logger.warning(
                    "RapidAPI irctc1 error: HTTP %s for train %s (%s->%s, %s on %s)",
                    status_code, train_no, from_station, to_station, class_type, travel_date_str,
                )
                return None
    except urllib.error.HTTPError as http_err:
        if http_err.code in (403, 429):
            logger.warning(
                "RapidAPI irctc1 HTTP error: HTTP %s (%s) for train %s (%s->%s, %s on %s) - falling back cleanly to segments.py",
                http_err.code, http_err.reason, train_no, from_station, to_station, class_type, travel_date_str,
            )
        else:
            logger.warning(
                "RapidAPI irctc1 HTTP error: HTTP %s (%s) for train %s (%s->%s, %s on %s)",
                http_err.code, http_err.reason, train_no, from_station, to_station, class_type, travel_date_str,
            )
        return None
    except Exception as exc:
        logger.warning(
            "RapidAPI irctc1 request exception for train %s (%s->%s, %s on %s): %s",
            train_no, from_station, to_station, class_type, travel_date_str, exc,
        )
        return None


def fetch_live_seat(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date: Union[date, str],
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
    allow_uncached_http: bool = True,
    db_path: Path = DB_PATH,
) -> Optional[Tuple[int, Optional[int]]]:
    """Retrieve seat availability for one train/from/to/class/date.

    1. Checks SQLite seat_cache table (6-hour TTL).
    2. If not cached, allow_uncached_http is True, and quota budget is available:
       Calls RapidAPI, records live call count, and saves to SQLite seat_cache.
    3. On HTTP 429/403 or when quota reached: returns None for clean fallback.
    """
    date_str = travel_date.isoformat() if isinstance(travel_date, date) else str(travel_date)

    # 1. Check SQLite seat_cache table (6-hour TTL)
    cached = get_cached_seat(
        train_no=train_no,
        from_station=from_station,
        to_station=to_station,
        class_type=class_type,
        travel_date=date_str,
        db_path=db_path,
    )
    if cached is not None:
        seats, fare = cached
        if fare is not None:
            LIVE_STRETCH_FARES[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = fare
        return cached

    # 2. Check API key and budget allowance
    if not api_key:
        api_key, env_url = get_api_config()
        if not api_url:
            api_url = env_url

    if not api_key or not allow_uncached_http or not can_make_live_call():
        return None

    # 3. Make HTTP call
    record_live_call()
    result = query_irctc1_api(
        train_no=train_no,
        from_station=from_station,
        to_station=to_station,
        class_type=class_type,
        travel_date_str=date_str,
        api_key=api_key,
        api_url=api_url,
        timeout=timeout,
    )
    if result is not None:
        seats, fare = result
        set_cached_seat(
            train_no=train_no,
            from_station=from_station,
            to_station=to_station,
            class_type=class_type,
            travel_date=date_str,
            seats=seats,
            fare=fare,
            db_path=db_path,
        )
        return seats, fare

    return None


def fetch_live_seats(
    train: str,
    from_station: str,
    to_station: str,
    travel_date: date,
    api_key: str,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
    class_types: Optional[List[str]] = None,
    allow_uncached_http: bool = True,
    db_path: Path = DB_PATH,
) -> Optional[Dict[str, int]]:
    """Fetch live seat counts mapping class -> seats left.

    Backward-compatible method returning dict of {class: seats}.
    """
    classes_to_check = class_types or ["3A", "2A", "SL"]
    res = {}
    for c in classes_to_check:
        got = fetch_live_seat(
            train_no=train,
            from_station=from_station,
            to_station=to_station,
            class_type=c,
            travel_date=travel_date,
            api_key=api_key,
            api_url=api_url,
            timeout=timeout,
            allow_uncached_http=allow_uncached_http,
            db_path=db_path,
        )
        if got is not None:
            seats, _ = got
            res[c] = seats
        elif not can_make_live_call() or not allow_uncached_http:
            break

    return res if res else None


def fallback_segment_seats(
    tt: Timetable,
    train: str,
    run_day: date,
    board_seq: int,
    alight_seq: int,
    leg_depart: int,
    board_dep: int,
    preferred_classes: Optional[Tuple[str, ...]] = None,
) -> Optional[LegSeatsResult]:
    """Simulated seat availability via segments.py."""
    from routing import Piece

    def seats_fn(cls, s):
        return seg.segment_seats(train, run_day, cls, s)

    classes = tuple(preferred_classes) if preferred_classes else None
    whole, plan = seg.class_plan(board_seq, alight_seq, seats_fn, classes=classes)
    if whole:
        if classes:
            whole = {c: n for c, n in whole.items() if c in classes}
        if whole:
            return LegSeatsResult(whole, (), source="simulated_estimate")
    if plan is None:
        return None
    stops = tt.stops_of(train)
    pieces = []
    for a, b, cls, n in plan:
        if classes and cls not in classes:
            return None
        sa, sb = stops[a], stops[b]
        dep_a = sa.dep if sa.dep is not None else sa.arr
        arr_b = sb.arr if sb.arr is not None else sb.dep
        pieces.append(Piece(sa.station, sb.station, cls, n,
                            leg_depart + dep_a - board_dep,
                            leg_depart + arr_b - board_dep,
                            availability_source="simulated_estimate",
                            source="simulated_estimate"))
    return LegSeatsResult({}, tuple(pieces), source="simulated_estimate")


def materialize_leg_seats(
    tt: Timetable,
    train: str,
    from_station: str,
    to_station: str,
    run_day: date,
    board_seq: int,
    alight_seq: int,
    leg_depart: int,
    board_dep: int,
    preferred_classes: Optional[Tuple[str, ...]] = None,
    allow_live: bool = True,
    db_path: Path = DB_PATH,
) -> Optional[LegSeatsResult]:
    """Determine seat availability for a candidate journey leg.

    If RAILWAY_API_KEY is configured and allow_live is True, queries RapidAPI (capped at 2
    uncached stretch checks per search, with 6-hour SQLite caching in trains.db).
    When RAILWAY_API_KEY is unset, call limit reached, or on HTTP 429/403, falls back
    seamlessly to segments.py.
    """
    api_key, api_url = get_api_config()
    classes = tuple(preferred_classes) if preferred_classes else None

    if api_key:
        t_classes = tt.train_classes(train) if hasattr(tt, "train_classes") else av.CLASSES
        candidate_classes = list(classes) if classes else list(t_classes)

        # Prioritize classes to check: 3A > SL > 2A > CC > 3E > 1A > EC > 2S
        if not classes:
            priority = ["3A", "SL", "2A", "CC", "3E", "1A", "EC", "2S"]
            candidate_classes.sort(key=lambda c: priority.index(c) if c in priority else 99)

        live_results = {}
        for c in candidate_classes:
            got = fetch_live_seat(
                train_no=train,
                from_station=from_station,
                to_station=to_station,
                class_type=c,
                travel_date=run_day,
                api_key=api_key,
                api_url=api_url,
                allow_uncached_http=allow_live,
                db_path=db_path,
            )
            if got is not None:
                seats, _ = got
                live_results[c] = seats
            else:
                # If uncached HTTP was not allowed, budget exhausted, or error occurred
                if not allow_live or not can_make_live_call():
                    break

        if live_results:
            available_classes = {c: n for c, n in live_results.items() if n > 0}
            if available_classes:
                return LegSeatsResult(available_classes, (), source="live_irctc")
            # All checked classes were waitlisted/full (0 available seats).
            # Do NOT fall back to segments.py to fabricate fake seats!
            logger.info(
                "RapidAPI irctc1: all checked classes %s for train %s (%s->%s on %s) are waitlisted/full (0 seats) - omitting train from available results",
                list(live_results.keys()), train, from_station, to_station, run_day,
            )
            return None

    # Seamless fallback to segments.py
    return fallback_segment_seats(
        tt=tt,
        train=train,
        run_day=run_day,
        board_seq=board_seq,
        alight_seq=alight_seq,
        leg_depart=leg_depart,
        board_dep=board_dep,
        preferred_classes=classes,
    )
