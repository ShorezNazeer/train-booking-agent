"""Pluggable live seat adapter with 15-minute in-memory TTL cache.

Reads RAILWAY_API_KEY and RAILWAY_API_URL from environment variables.
When RAILWAY_API_KEY is unset, falls back seamlessly to segments.py.
Called on final candidate journeys in routing._materialize().
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Dict, Optional, Tuple

import availability as av
import segments as seg
from data_source import Timetable

logger = logging.getLogger(__name__)

TTL_SECONDS = 15 * 60  # 15 minutes

# In-memory TTL cache: key -> (timestamp, seat_dict)
# Key format: (train, from_station, to_station, date_str)
_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, Dict[str, int]]] = {}


def get_api_config() -> Tuple[Optional[str], Optional[str]]:
    """Return (api_key, api_url) from environment variables."""
    key = os.environ.get("RAILWAY_API_KEY", "").strip() or None
    url = os.environ.get("RAILWAY_API_URL", "").strip() or None
    return key, url


def clear_cache():
    """Clear all cached seat availability."""
    _CACHE.clear()


def get_cache() -> Dict[Tuple[str, str, str, str], Tuple[float, Dict[str, int]]]:
    """Return the raw in-memory cache dict."""
    return _CACHE


def fetch_live_seats(
    train: str,
    from_station: str,
    to_station: str,
    travel_date: date,
    api_key: str,
    api_url: Optional[str] = None,
    timeout: float = 5.0,
) -> Optional[Dict[str, int]]:
    """Fetch live seat counts from the external Railway API with 15-minute TTL caching.

    Returns a dict mapping travel class to seats left, e.g. {"1A": 5, "2A": 12, "3A": 20, "SL": 50},
    or None if unavailable / error.
    """
    date_str = travel_date.isoformat() if isinstance(travel_date, date) else str(travel_date)
    cache_key = (str(train), str(from_station), str(to_station), date_str)
    now = time.time()

    # Check TTL cache
    if cache_key in _CACHE:
        ts, cached_data = _CACHE[cache_key]
        if now - ts < TTL_SECONDS:
            return cached_data
        else:
            del _CACHE[cache_key]

    base_url = (api_url or "https://api.railwayapi.site/v1").rstrip("/")
    params = urllib.parse.urlencode({
        "train": train,
        "from": from_station,
        "to": to_station,
        "date": date_str,
    })
    req_url = f"{base_url}/seats?{params}" if "/seats" not in base_url else f"{base_url}?{params}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
        "User-Agent": "TrainBookingAgent/1.0",
        "Accept": "application/json",
    }

    req = urllib.request.Request(req_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                payload = json.loads(body)
                # Parse seats from payload
                seats_data = payload.get("seats") if isinstance(payload, dict) and "seats" in payload else payload
                if isinstance(seats_data, dict):
                    res = {str(k): int(v) for k, v in seats_data.items() if isinstance(v, (int, float))}
                    _CACHE[cache_key] = (now, res)
                    return res
    except Exception as exc:
        logger.warning("Live seat API request failed for %s (%s -> %s on %s): %s",
                       train, from_station, to_station, date_str, exc)

    return None


def fallback_segment_seats(
    tt: Timetable,
    train: str,
    run_day: date,
    board_seq: int,
    alight_seq: int,
    leg_depart: int,
    board_dep: int,
) -> Optional[Tuple[Dict[str, int], Tuple]]:
    """Simulated seat availability via segments.py."""
    from routing import Piece

    def seats_fn(cls, s):
        return seg.segment_seats(train, run_day, cls, s)

    whole, plan = seg.class_plan(board_seq, alight_seq, seats_fn)
    if whole:
        return whole, ()
    if plan is None:
        return None
    stops = tt.stops_of(train)
    pieces = []
    for a, b, cls, n in plan:
        sa, sb = stops[a], stops[b]
        dep_a = sa.dep if sa.dep is not None else sa.arr
        arr_b = sb.arr if sb.arr is not None else sb.dep
        pieces.append(Piece(sa.station, sb.station, cls, n,
                            leg_depart + dep_a - board_dep,
                            leg_depart + arr_b - board_dep))
    return {}, tuple(pieces)


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
) -> Optional[Tuple[Dict[str, int], Tuple]]:
    """Determine seat availability for a candidate journey leg.

    If RAILWAY_API_KEY is configured, queries the live railway API (with 15-min TTL cache).
    When RAILWAY_API_KEY is unset or API call fails, falls back seamlessly to segments.py.
    """
    api_key, api_url = get_api_config()

    if api_key:
        live = fetch_live_seats(
            train=train,
            from_station=from_station,
            to_station=to_station,
            travel_date=run_day,
            api_key=api_key,
            api_url=api_url,
        )
        if live is not None:
            # If live seats exist and has availability, return it
            available_classes = {c: n for c, n in live.items() if n > 0}
            if available_classes:
                return available_classes, ()
            # If live seats explicitly returned 0 seats for all classes, check if any class has 0
            if live:
                return {}, ()

    # Seamless fallback to segments.py
    return fallback_segment_seats(
        tt=tt,
        train=train,
        run_day=run_day,
        board_seq=board_seq,
        alight_seq=alight_seq,
        leg_depart=leg_depart,
        board_dep=board_dep,
    )
