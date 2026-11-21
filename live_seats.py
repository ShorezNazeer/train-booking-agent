"""Live seat availability adapter connecting to the RailCore API (https://ir.railcore.tech/v1/availability/seats).

Features:
- Connects to RailCore API via GET /v1/availability/seats when RAILCORE_API_KEY
  (or fallback RAILWAY_API_KEY) is configured.
- Sends headers: X-RailCore-Key and Accept: application/json.
- Query parameters: train_number, from, to, date (strict YYYY-MM-DD), class, quota='GN'.
- Parses RailCore response envelope (success, data.classes[], data.enquiry_checked_at):
  - Maps status == 'AVAILABLE' (using available_count, defaulting to 1 if null) to confirmed bookable seats.
  - Waitlist Safety Guard: omits train (returns None) if status is WAITLIST, RAC, REGRET,
    NOT_AVAILABLE, TRAIN_CANCELLED, TRAIN_DEPARTED, or UNKNOWN so routing.py never falls back to segments.py
    to fabricate fake seats.
- Credit Protection:
  - Single primary class queried per leg (never loops across multiple classes, preventing per-class credit multiplication).
  - MAX_LIVE_CALLS_PER_SEARCH = 1: at most 1 uncached live check per search.
  - Daily quota tracked in SQLite `api_daily_usage` (IST date) capped by MAX_DAILY_LIVE_CALLS (default 250, < 300 Free tier limit).
    Counter increments only on HTTP 200 responses (failed 4xx/5xx cost 0 credits).
  - 6-hour TTL on SQLite `seat_cache` table inside data/trains.db.
- Clean fallback to segments.py with availability_source='simulated_estimate' when API key is unset,
  quota exhausted, or on HTTP 4xx/5xx errors.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import availability as av
from data_source import DB_PATH, Timetable
import segments as seg

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

RAILCORE_DEFAULT_URL = "https://ir.railcore.tech/v1/availability/seats"
SEAT_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours TTL (<= 24h)
MAX_LIVE_CALLS_PER_SEARCH = 1      # Spend at most 1 credit per search
DEFAULT_MAX_DAILY_LIVE_CALLS = 250 # Below RailCore 300/day Free limit

# Global search session tracking: uncached HTTP calls made during current search
_uncached_calls_in_search: int = 0

# In-memory cache for fast lookup: (train, from_station, to_station, class_type, date_str) -> (timestamp, seats, fare, status_str, fetched_at)
_CACHE: Dict[Tuple[str, str, str, str, str], Tuple[float, int, Optional[int], str, Optional[str]]] = {}

# Recorded live stretch fares and enquiry check timestamps
LIVE_STRETCH_FARES: Dict[Tuple[str, str, str], int] = {}
LIVE_STRETCH_FETCHED_AT: Dict[Tuple[str, str, str], str] = {}


class LegSeatsResult(tuple):
    """A 2-tuple (seats, split) carrying availability source ('live_railcore' or 'simulated_estimate')."""
    source: str
    availability_source: str
    fetched_at: Optional[str]

    def __new__(cls, seats: dict, split: tuple = (), source: str = "simulated_estimate", fetched_at: Optional[str] = None):
        obj = super().__new__(cls, (seats, split))
        obj.source = source
        obj.availability_source = source
        obj.fetched_at = fetched_at
        return obj


class CachedSeat(tuple):
    """2-tuple (seats, fare) with status_str and fetched_at attributes."""
    seats: int
    fare: Optional[int]
    status_str: str
    fetched_at: Optional[str]

    def __new__(cls, seats: int, fare: Optional[int] = None, status_str: str = "", fetched_at: Optional[str] = None):
        obj = super().__new__(cls, (int(seats), int(fare) if fare is not None else None))
        obj.seats = int(seats)
        obj.fare = int(fare) if fare is not None else None
        obj.status_str = status_str
        obj.fetched_at = fetched_at
        return obj


def normalize_iso_date(date_val: Union[str, date, datetime]) -> str:
    """Standardize date to strict YYYY-MM-DD format.

    Accepts date/datetime objects, YYYY-MM-DD, DD-MM-YYYY, or DD/MM/YYYY.
    Raises ValueError on invalid formats.
    """
    if isinstance(date_val, (date, datetime)):
        return date_val.strftime("%Y-%m-%d")
    s = str(date_val).strip()
    if not s:
        raise ValueError("Empty date string")
    # Strict regex check for YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        # Validate calendar validity
        datetime.strptime(s, "%Y-%m-%d")
        return s

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%Y/%m/%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"Unable to parse date '{date_val}' into YYYY-MM-DD format")


def get_ist_date_str() -> str:
    """Return current date in Indian Standard Time (UTC+05:30) as YYYY-MM-DD."""
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    return ist_now.strftime("%Y-%m-%d")


def get_max_daily_calls() -> int:
    """Return maximum allowed live calls per day from environment."""
    try:
        return int(os.environ.get("MAX_DAILY_LIVE_CALLS", str(DEFAULT_MAX_DAILY_LIVE_CALLS)))
    except (ValueError, TypeError):
        return DEFAULT_MAX_DAILY_LIVE_CALLS


def init_seat_cache_db(db_path: Path = DB_PATH) -> None:
    """Ensure the seat_cache and api_daily_usage tables exist in SQLite database."""
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
                        status_str TEXT,
                        fetched_at TEXT,
                        PRIMARY KEY (train_no, from_station, to_station, class_type, travel_date)
                    )
                    """
                )
                try:
                    conn.execute("ALTER TABLE seat_cache ADD COLUMN status_str TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE seat_cache ADD COLUMN fetched_at TEXT")
                except sqlite3.OperationalError:
                    pass

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_daily_usage (
                        usage_date TEXT PRIMARY KEY,
                        call_count INTEGER DEFAULT 0
                    )
                    """
                )
                conn.commit()
    except Exception as exc:
        logger.warning("Could not initialize seat_cache / api_daily_usage table: %s", exc)


# Initialize tables on module load
init_seat_cache_db()


def get_api_config() -> Tuple[Optional[str], Optional[str]]:
    """Return (api_key, api_url) from environment variables.

    Reads RAILCORE_API_KEY with fallback to RAILWAY_API_KEY.
    """
    key = (
        os.environ.get("RAILCORE_API_KEY", "").strip()
        or os.environ.get("RAILWAY_API_KEY", "").strip()
        or None
    )
    url = (
        os.environ.get("RAILCORE_API_URL", "").strip()
        or os.environ.get("RAILWAY_API_URL", "").strip()
        or RAILCORE_DEFAULT_URL
    )
    return key, url


def log_startup_api_key() -> bool:
    """Log whether RAILCORE_API_KEY (or fallback RAILWAY_API_KEY) is detected at startup."""
    key, _ = get_api_config()
    if key:
        masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
        logger.info("RailCore Diagnostics: API key detected at startup: YES (%s, len=%d)", masked, len(key))
        return True
    else:
        logger.info("RailCore Diagnostics: API key detected at startup: NO (unset, using offline simulated estimates)")
        return False


# Log startup API key status on module import
log_startup_api_key()


def reset_search_quota() -> None:
    """Reset the uncached call counter for a new search."""
    global _uncached_calls_in_search
    _uncached_calls_in_search = 0


def get_uncached_call_count() -> int:
    """Return the number of uncached HTTP calls made in the current search."""
    return _uncached_calls_in_search


def record_live_call() -> None:
    """Increment the uncached live call counter for the current search."""
    global _uncached_calls_in_search
    _uncached_calls_in_search += 1


def get_daily_usage(usage_date: Optional[str] = None, db_path: Path = DB_PATH) -> int:
    """Get current day live API calls count recorded in SQLite api_daily_usage."""
    date_str = usage_date or get_ist_date_str()
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                row = conn.execute(
                    "SELECT call_count FROM api_daily_usage WHERE usage_date = ?",
                    (date_str,),
                ).fetchone()
                if row:
                    return int(row[0])
    except Exception as exc:
        logger.warning("api_daily_usage read error: %s", exc)
    return 0


def record_daily_live_call(usage_date: Optional[str] = None, db_path: Path = DB_PATH) -> int:
    """Increment daily live call counter in SQLite (only on HTTP 200 responses)."""
    date_str = usage_date or get_ist_date_str()
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT INTO api_daily_usage (usage_date, call_count)
                    VALUES (?, 1)
                    ON CONFLICT(usage_date) DO UPDATE SET call_count = call_count + 1
                    """,
                    (date_str,),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT call_count FROM api_daily_usage WHERE usage_date = ?",
                    (date_str,),
                ).fetchone()
                return int(row[0]) if row else 1
    except Exception as exc:
        logger.warning("api_daily_usage increment error: %s", exc)
    return 0


def is_daily_quota_available(max_daily: Optional[int] = None, db_path: Path = DB_PATH) -> bool:
    """Check whether today's live call count is below the configured daily limit."""
    limit = max_daily if max_daily is not None else get_max_daily_calls()
    current_usage = get_daily_usage(db_path=db_path)
    return current_usage < limit


def can_make_live_call(db_path: Path = DB_PATH) -> bool:
    """Check if we have uncached call budget in current search AND daily quota is available."""
    if _uncached_calls_in_search >= MAX_LIVE_CALLS_PER_SEARCH:
        return False
    if not is_daily_quota_available(db_path=db_path):
        logger.warning("RailCore daily live API quota reached (>= %d calls today)", get_max_daily_calls())
        return False
    return True


def clear_cache(db_path: Path = DB_PATH) -> None:
    """Clear memory and SQLite seat cache."""
    _CACHE.clear()
    LIVE_STRETCH_FARES.clear()
    LIVE_STRETCH_FETCHED_AT.clear()
    reset_search_quota()
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute("DELETE FROM seat_cache")
                conn.commit()
    except Exception as exc:
        logger.warning("Failed clearing seat_cache table: %s", exc)


def clear_daily_usage(db_path: Path = DB_PATH) -> None:
    """Clear api_daily_usage table (for tests)."""
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute("DELETE FROM api_daily_usage")
                conn.commit()
    except Exception as exc:
        logger.warning("Failed clearing api_daily_usage table: %s", exc)


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


def get_live_fetched_at(
    from_station: str,
    to_station: str,
    class_type: str,
) -> Optional[str]:
    """Retrieve recorded live enquiry checked timestamp for a stretch."""
    k = (str(from_station).upper(), str(to_station).upper(), str(class_type).upper())
    return LIVE_STRETCH_FETCHED_AT.get(k)


def get_cached_seat(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date: Union[date, str],
    db_path: Path = DB_PATH,
    ttl: float = SEAT_CACHE_TTL_SECONDS,
) -> Optional[CachedSeat]:
    """Retrieve (seats, fare) from memory or SQLite seat_cache if within TTL."""
    try:
        norm_date = normalize_iso_date(travel_date)
    except ValueError:
        norm_date = str(travel_date)

    cache_key = (str(train_no), str(from_station), str(to_station), str(class_type), norm_date)
    now = time.time()

    # Check memory cache
    if cache_key in _CACHE:
        ts, seats, fare, status_str, fetched_at = _CACHE[cache_key]
        if now - ts < ttl:
            return CachedSeat(seats, fare, status_str=status_str, fetched_at=fetched_at)
        else:
            del _CACHE[cache_key]

    # Check SQLite seat_cache table
    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                row = conn.execute(
                    """
                    SELECT seats, fare, cached_at, status_str, fetched_at FROM seat_cache
                    WHERE train_no = ? AND from_station = ? AND to_station = ?
                      AND class_type = ? AND travel_date = ?
                    """,
                    cache_key,
                ).fetchone()
                if row:
                    seats, fare, cached_at, status_str, fetched_at = row
                    fare_int = int(fare) if fare is not None else None
                    if cached_at is not None and (now - float(cached_at) < ttl):
                        _CACHE[cache_key] = (float(cached_at), int(seats), fare_int, str(status_str or ""), fetched_at)
                        return CachedSeat(int(seats), fare_int, status_str=str(status_str or ""), fetched_at=fetched_at)
    except Exception as exc:
        logger.warning("seat_cache read error: %s", exc)

    return None


def set_cached_seat(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date: Union[date, str],
    seats: int,
    fare: Optional[int],
    cached_at: Optional[float] = None,
    db_path: Path = DB_PATH,
    status_str: Optional[str] = None,
    fetched_at: Optional[str] = None,
) -> None:
    """Store (seats, fare) in memory and SQLite seat_cache table with timestamp."""
    try:
        norm_date = normalize_iso_date(travel_date)
    except ValueError:
        norm_date = str(travel_date)

    now = time.time()
    if cached_at is None:
        cached_at = now
    if fetched_at is None:
        fetched_at = datetime.fromtimestamp(cached_at, tz=timezone.utc).isoformat()

    cache_key = (str(train_no), str(from_station), str(to_station), str(class_type), norm_date)
    fare_int = int(fare) if fare is not None else None
    status_text = str(status_str or ("AVAILABLE" if seats > 0 else "WAITLIST"))
    _CACHE[cache_key] = (float(cached_at), int(seats), fare_int, status_text, fetched_at)

    try:
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seat_cache
                    (train_no, from_station, to_station, class_type, travel_date, seats, fare, cached_at, status_str, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(train_no),
                        str(from_station),
                        str(to_station),
                        str(class_type),
                        norm_date,
                        int(seats),
                        fare_int,
                        float(cached_at),
                        status_text,
                        fetched_at,
                    ),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("seat_cache write error: %s", exc)


def parse_railcore_response(
    payload: dict,
    class_type: str,
) -> Optional[Tuple[int, Optional[int], str, Optional[str]]]:
    """Parse RailCore JSON response envelope.

    Envelope format:
    {
        "success": true,
        "data": {
            "enquiry_checked_at": "2026-11-21T18:00:00Z",
            "classes": [
                {
                    "class": "3A",
                    "status": "AVAILABLE",
                    "available_count": 42,
                    "total_fare": 695
                }
            ]
        }
    }

    Returns:
        (seats, total_fare, status_str, enquiry_checked_at) or None on failure.
    """
    if not isinstance(payload, dict):
        return None

    if "success" in payload and not payload.get("success"):
        logger.warning("RailCore API returned success=False: %s", payload.get("message") or payload.get("error"))
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        # Support root-level classes if formatted that way
        if isinstance(payload.get("classes"), list):
            data = payload
        else:
            return None

    enquiry_checked_at = data.get("enquiry_checked_at")
    classes_list = data.get("classes", [])
    if not isinstance(classes_list, list) or not classes_list:
        return None

    target_class = class_type.strip().upper()
    matched_entry = None
    for item in classes_list:
        if isinstance(item, dict):
            c_code = (item.get("class") or item.get("class_type") or item.get("class_code") or "").strip().upper()
            if c_code == target_class:
                matched_entry = item
                break

    # If only one class in list, accept it as the queried class
    if matched_entry is None and len(classes_list) == 1 and isinstance(classes_list[0], dict):
        matched_entry = classes_list[0]

    if matched_entry is None:
        logger.warning("Target class %s not found in RailCore classes payload", class_type)
        return None

    raw_status = str(matched_entry.get("status") or "").strip().upper()
    avail_count = matched_entry.get("available_count")

    # Map status == 'AVAILABLE' to confirmed bookable seats
    if raw_status == "AVAILABLE":
        if avail_count is not None:
            try:
                seats = int(avail_count)
            except (ValueError, TypeError):
                seats = 1
        else:
            seats = 1
    else:
        # WAITLIST, RAC, REGRET, NOT_AVAILABLE, TRAIN_CANCELLED, TRAIN_DEPARTED, UNKNOWN -> 0 seats
        seats = 0

    fare = None
    raw_fare = matched_entry.get("total_fare") or matched_entry.get("ticket_fare") or matched_entry.get("fare")
    if raw_fare is None and "total_fare" in data:
        raw_fare = data.get("total_fare")
    if raw_fare is not None:
        try:
            fare = int(raw_fare)
        except (ValueError, TypeError):
            fare = None

    return seats, fare, raw_status, enquiry_checked_at


def query_railcore_api(
    train_no: str,
    from_station: str,
    to_station: str,
    class_type: str,
    travel_date_str: str,
    api_key: str,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
    db_path: Path = DB_PATH,
) -> Optional[Tuple[int, Optional[int], str, Optional[str]]]:
    """Execute GET request to RailCore API (/v1/availability/seats).

    Query params: train_number, from, to, date (YYYY-MM-DD), class, quota=GN.
    Headers: X-RailCore-Key, Accept: application/json.
    Increments daily usage counter only on HTTP 200 responses.
    """
    base_url = (api_url or os.getenv("RAILCORE_API_URL") or os.getenv("RAILWAY_API_URL") or RAILCORE_DEFAULT_URL).strip()
    norm_date = normalize_iso_date(travel_date_str)

    params = urllib.parse.urlencode({
        "train_number": str(train_no),
        "from": str(from_station).upper(),
        "to": str(to_station).upper(),
        "date": norm_date,
        "class": str(class_type).upper(),
        "quota": "GN",
    })
    req_url = f"{base_url}?{params}" if "?" not in base_url else f"{base_url}&{params}"

    headers = {
        "X-RailCore-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "TrainBookingAgent/2.0",
    }

    req = urllib.request.Request(req_url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = getattr(resp, "status", getattr(resp, "code", 200))
            if status_code == 200:
                # Only increment daily live call counter on HTTP 200 (failed 4xx/5xx cost 0 credits)
                record_daily_live_call(db_path=db_path)

                body = resp.read().decode("utf-8")
                payload = json.loads(body)
                parsed = parse_railcore_response(payload, class_type=class_type)
                if parsed is not None:
                    seats, fare, status_str, enquiry_checked_at = parsed
                    logger.info(
                        "RailCore API response: HTTP 200 for train %s (%s->%s, %s on %s) - status: '%s', available_count: %s, checked_at: %s",
                        train_no, from_station, to_station, class_type, norm_date, status_str, seats, enquiry_checked_at,
                    )
                    return parsed
                else:
                    logger.warning("RailCore API response could not be parsed: %s", payload)
                    return None
            else:
                logger.warning(
                    "RailCore API returned HTTP %s for train %s (%s->%s, %s on %s)",
                    status_code, train_no, from_station, to_station, class_type, norm_date,
                )
                return None
    except urllib.error.HTTPError as http_err:
        logger.warning(
            "RailCore API HTTP error: HTTP %s (%s) for train %s (%s->%s, %s on %s)",
            http_err.code, http_err.reason, train_no, from_station, to_station, class_type, norm_date,
        )
        return None
    except Exception as exc:
        logger.warning(
            "RailCore API request exception for train %s (%s->%s, %s on %s): %s",
            train_no, from_station, to_station, class_type, norm_date, exc,
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
) -> Optional[CachedSeat]:
    """Retrieve seat availability for one train/from/to/class/date.

    1. Checks SQLite seat_cache table (6-hour TTL).
    2. If not cached, allow_uncached_http is True, and quota budget is available:
       Calls RailCore API, records live search call count, and saves to SQLite seat_cache.
    3. On HTTP errors or when quota reached: returns None for clean fallback.
    """
    date_str = normalize_iso_date(travel_date)

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
        if cached.fare is not None:
            LIVE_STRETCH_FARES[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = cached.fare
        if cached.fetched_at:
            LIVE_STRETCH_FETCHED_AT[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = cached.fetched_at
        return cached

    # 2. Check API key and budget allowance
    if not api_key:
        api_key, env_url = get_api_config()
        if not api_url:
            api_url = env_url

    if not api_key or not allow_uncached_http or not can_make_live_call(db_path=db_path):
        return None

    # 3. Make live HTTP call to RailCore
    record_live_call()
    result = query_railcore_api(
        train_no=train_no,
        from_station=from_station,
        to_station=to_station,
        class_type=class_type,
        travel_date_str=date_str,
        api_key=api_key,
        api_url=api_url,
        timeout=timeout,
        db_path=db_path,
    )
    if result is not None:
        seats, fare, status_str, enquiry_checked_at = result
        if fare is not None:
            LIVE_STRETCH_FARES[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = fare
        if enquiry_checked_at:
            LIVE_STRETCH_FETCHED_AT[(str(from_station).upper(), str(to_station).upper(), str(class_type).upper())] = enquiry_checked_at

        set_cached_seat(
            train_no=train_no,
            from_station=from_station,
            to_station=to_station,
            class_type=class_type,
            travel_date=date_str,
            seats=seats,
            fare=fare,
            db_path=db_path,
            status_str=status_str,
            fetched_at=enquiry_checked_at,
        )
        return CachedSeat(seats, fare, status_str=status_str, fetched_at=enquiry_checked_at)

    return None


def fetch_live_seats(
    train: str,
    from_station: str,
    to_station: str,
    travel_date: Union[date, str],
    api_key: str,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
    class_types: Optional[List[str]] = None,
    allow_uncached_http: bool = True,
    db_path: Path = DB_PATH,
) -> Optional[Dict[str, int]]:
    """Fetch live seat counts mapping class -> seats left (backward compatible utility)."""
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
            res[c] = got.seats
        elif not can_make_live_call(db_path=db_path) or not allow_uncached_http:
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

    If RAILCORE_API_KEY (or RAILWAY_API_KEY) is configured and allow_live is True, queries
    RailCore API for the single primary class (preventing per-class credit multiplication,
    capped at 1 uncached live check per search and 250 calls/day in SQLite trains.db).

    Waitlist Safety Guard:
    If RailCore returns WAITLIST, RAC, REGRET, NOT_AVAILABLE, TRAIN_CANCELLED, TRAIN_DEPARTED,
    or UNKNOWN (0 confirmed seats), returns None so routing.py omits the train instead of
    falling back to segments.py to fabricate fake seats.

    When API key is unset, call limit reached, or on HTTP 4xx/5xx, falls back seamlessly
    to segments.py (tagged as 'simulated_estimate').
    """
    api_key, api_url = get_api_config()
    classes = tuple(preferred_classes) if preferred_classes else None

    if api_key:
        t_classes = tt.train_classes(train) if hasattr(tt, "train_classes") else av.CLASSES
        candidate_classes = list(classes) if classes else list(t_classes)

        # Prioritize single primary class: 3A > SL > 2A > CC > 3E > 1A > EC > 2S
        priority = ["3A", "SL", "2A", "CC", "3E", "1A", "EC", "2S"]
        if not classes:
            candidate_classes.sort(key=lambda c: priority.index(c) if c in priority else 99)

        if candidate_classes:
            # Query ONLY the single primary class to prevent per-class credit multiplication
            primary_class = candidate_classes[0]
            got = fetch_live_seat(
                train_no=train,
                from_station=from_station,
                to_station=to_station,
                class_type=primary_class,
                travel_date=run_day,
                api_key=api_key,
                api_url=api_url,
                allow_uncached_http=allow_live,
                db_path=db_path,
            )
            if got is not None:
                seats = got.seats
                status_str = got.status_str
                fetched_at = got.fetched_at

                if seats > 0 and status_str == "AVAILABLE":
                    return LegSeatsResult({primary_class: seats}, (), source="live_railcore", fetched_at=fetched_at)
                else:
                    # Waitlist Safety Guard: omit train rather than fabricating fake seats from segments.py
                    logger.info(
                        "RailCore live availability: train %s (%s->%s, %s on %s) status is '%s' (0 confirmed seats) - omitting train",
                        train, from_station, to_station, primary_class, run_day, status_str,
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
