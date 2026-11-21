import json
import sqlite3
import time
import urllib.error
import urllib.parse
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import fare_engine
import live_seats
from api import app
from data_source import DB_PATH, Timetable


@pytest.fixture(autouse=True)
def clean_cache():
    live_seats.clear_cache()
    live_seats.clear_daily_usage()
    yield
    live_seats.clear_cache()
    live_seats.clear_daily_usage()


def test_normalize_iso_date():
    """Verify normalize_iso_date strictly standardizes to YYYY-MM-DD."""
    assert live_seats.normalize_iso_date("2026-11-20") == "2026-11-20"
    assert live_seats.normalize_iso_date("20-11-2026") == "2026-11-20"
    assert live_seats.normalize_iso_date("20/11/2026") == "2026-11-20"
    assert live_seats.normalize_iso_date("20-Nov-2026") == "2026-11-20"
    assert live_seats.normalize_iso_date("20 Nov 2026") == "2026-11-20"
    assert live_seats.normalize_iso_date(date(2026, 11, 20)) == "2026-11-20"
    assert live_seats.normalize_iso_date(datetime(2026, 11, 20, 14, 30)) == "2026-11-20"

    with pytest.raises(ValueError):
        live_seats.normalize_iso_date("invalid-date")
    with pytest.raises(ValueError):
        live_seats.normalize_iso_date("")


def test_live_seats_fallback_when_key_unset(monkeypatch):
    """When API keys are unset, falls back cleanly to segments.py with source simulated_estimate."""
    monkeypatch.delenv("RAILCORE_API_KEY", raising=False)
    monkeypatch.delenv("RAILWAY_API_KEY", raising=False)
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A", "2A", "SL"]

    with patch("live_seats.fallback_segment_seats", return_value=live_seats.LegSeatsResult({"3A": 10}, (), source="simulated_estimate")) as mock_fallback:
        res = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=5,
            leg_depart=375,
            board_dep=375,
        )
        assert res == ({"3A": 10}, ())
        assert res.source == "simulated_estimate"
        assert res.availability_source == "simulated_estimate"
        mock_fallback.assert_called_once()


def test_railcore_api_request_and_response_available(monkeypatch):
    """Verify RailCore API headers, query parameters, response parsing, and persistence."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["CC", "EC"]

    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T10:15:00Z",
            "classes": [
                {
                    "class": "CC",
                    "status": "AVAILABLE",
                    "available_count": 42,
                    "total_fare": 695,
                }
            ],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=5,
            leg_depart=375,
            board_dep=375,
            preferred_classes=("CC",),
        )
        assert res is not None
        seats, split = res
        assert seats == {"CC": 42}
        assert split == ()
        assert res.source == "live_railcore"
        assert res.availability_source == "live_railcore"
        assert res.fetched_at == "2026-11-20T10:15:00Z"
        assert mock_urlopen.call_count == 1

        # Verify request headers and query parameters sent to RailCore
        call_args = mock_urlopen.call_args[0]
        req = call_args[0]
        assert req.headers["X-railcore-key"] == "rc_test_key_abc123"
        assert req.headers["Accept"] == "application/json"

        parsed_url = urllib.parse.urlparse(req.full_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        assert params["train_number"] == ["12004"]
        assert params["from"] == ["NDLS"]
        assert params["to"] == ["CNB"]
        assert params["class"] == ["CC"]
        assert params["quota"] == ["GN"]
        assert params["date"] == ["2026-11-20"]

        # Verify stretch fare updated with total_fare (695)
        live_fare = live_seats.get_live_fare("NDLS", "CNB", "CC")
        assert live_fare == 695
        sf = fare_engine.calculate_stretch_fare("NDLS", "CNB", "CC")
        assert sf["total_fare"] == 695

        # Verify SQLite persistence in seat_cache table
        cached_seat = live_seats.get_cached_seat("12004", "NDLS", "CNB", "CC", "2026-11-20")
        assert cached_seat is not None
        c_seats, c_fare = cached_seat
        assert c_seats == 42
        assert c_fare == 695
        assert cached_seat.status_str == "AVAILABLE"
        assert cached_seat.fetched_at == "2026-11-20T10:15:00Z"


def test_railcore_single_primary_class_queried(monkeypatch):
    """Verify only the single primary class is queried per leg to prevent credit multiplication."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    # Train has 4 candidate classes
    tt.train_classes.return_value = ["3A", "2A", "1A", "SL"]

    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T12:00:00Z",
            "classes": [
                {
                    "class": "3A",
                    "status": "AVAILABLE",
                    "available_count": 25,
                    "total_fare": 750,
                }
            ],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res = live_seats.materialize_leg_seats(
            tt=tt,
            train="12424",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=2,
            leg_depart=980,
            board_dep=980,
            allow_live=True,
        )
        assert res is not None
        # Exactly 1 HTTP call made for 3A (no extra credit spending for 2A/1A/SL)
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        assert params["class"] == ["3A"]


def test_railcore_waitlist_safety_guard(monkeypatch):
    """When RailCore returns waitlist/RAC/regret/cancelled, return None and omit train."""
    statuses = ["WAITLIST", "RAC", "REGRET", "NOT_AVAILABLE", "TRAIN_CANCELLED", "TRAIN_DEPARTED", "UNKNOWN"]
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    for st in statuses:
        live_seats.clear_cache()
        mock_payload = {
            "success": True,
            "data": {
                "enquiry_checked_at": "2026-11-20T12:00:00Z",
                "classes": [
                    {
                        "class": "3A",
                        "status": st,
                        "available_count": 0,
                        "total_fare": 750,
                    }
                ],
            },
        }
        mock_body = json.dumps(mock_payload).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = mock_body
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("live_seats.fallback_segment_seats") as mock_fallback:
            res = live_seats.materialize_leg_seats(
                tt=tt,
                train="12424",
                from_station="NDLS",
                to_station="CNB",
                run_day=date(2026, 11, 20),
                board_seq=0,
                alight_seq=2,
                leg_depart=980,
                board_dep=980,
                allow_live=True,
            )
            # Must return None so train is omitted; NEVER call fallback to fake seats!
            assert res is None, f"Status {st} should have returned None"
            mock_fallback.assert_not_called()


def test_railcore_sqlite_seat_cache_6hour_ttl(monkeypatch):
    """Test 6-hour TTL expiration on SQLite seat cache."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T12:00:00Z",
            "classes": [
                {
                    "class": "3A",
                    "status": "AVAILABLE",
                    "available_count": 15,
                    "total_fare": 785,
                }
            ],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        # First call fetches from API
        res1 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12424",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=2,
            leg_depart=980,
            board_dep=980,
            preferred_classes=("3A",),
        )
        assert res1 == ({"3A": 15}, ())
        assert mock_urlopen.call_count == 1

        # Second call immediately should hit SQLite/memory cache (0 additional urlopen calls)
        res2 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12424",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=2,
            leg_depart=980,
            board_dep=980,
            preferred_classes=("3A",),
        )
        assert res2 == res1
        assert mock_urlopen.call_count == 1

        # Simulate restart by clearing in-memory cache
        live_seats.get_cache().clear()
        res3 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12424",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=2,
            leg_depart=980,
            board_dep=980,
            preferred_classes=("3A",),
        )
        assert res3 == res1
        assert mock_urlopen.call_count == 1  # Served from SQLite!

        # Simulate 7 hours passing (> 6-hour TTL) in SQLite
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE seat_cache SET cached_at = cached_at - ? WHERE train_no = ?",
                (7 * 3600, "12424"),
            )
            conn.commit()
        live_seats.get_cache().clear()
        live_seats.reset_search_quota()

        # Call after 7 hours re-fetches from API
        res4 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12424",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=2,
            leg_depart=980,
            board_dep=980,
            preferred_classes=("3A",),
        )
        assert res4 == res1
        assert mock_urlopen.call_count == 2  # Re-fetched because TTL expired


def test_railcore_max_live_calls_per_search_cap_1(monkeypatch):
    """Verify live HTTP calls are capped at 1 uncached check per search."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    live_seats.clear_cache()
    live_seats.reset_search_quota()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T12:00:00Z",
            "classes": [
                {
                    "class": "3A",
                    "status": "AVAILABLE",
                    "available_count": 50,
                    "total_fare": 650,
                }
            ],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen, \
         patch("live_seats.fallback_segment_seats", return_value=live_seats.LegSeatsResult({"3A": 15}, (), source="simulated_estimate")) as mock_fallback:

        # 1. Non-top journey (allow_live=False) does NOT make uncached HTTP calls
        res_non_top = live_seats.materialize_leg_seats(
            tt=tt,
            train="22436",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=1,
            leg_depart=360,
            board_dep=360,
            preferred_classes=("3A",),
            allow_live=False,
        )
        assert res_non_top == ({"3A": 15}, ())
        assert mock_urlopen.call_count == 0
        assert mock_fallback.call_count == 1

        # 2. First live check succeeds (uncached call 1)
        res_top1 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=5,
            leg_depart=375,
            board_dep=375,
            preferred_classes=("3A",),
            allow_live=True,
        )
        assert res_top1 == ({"3A": 50}, ())
        assert mock_urlopen.call_count == 1
        assert live_seats.get_uncached_call_count() == 1

        # 3. Second uncached check in the same search must be blocked by MAX_LIVE_CALLS_PER_SEARCH = 1
        assert not live_seats.can_make_live_call()
        res_top2 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="CNB",
            to_station="LKO",
            run_day=date(2026, 11, 20),
            board_seq=5,
            alight_seq=8,
            leg_depart=690,
            board_dep=690,
            preferred_classes=("3A",),
            allow_live=True,
        )
        assert res_top2 == ({"3A": 15}, ())
        assert mock_urlopen.call_count == 1  # Capped at 1! No second HTTP call!
        assert mock_fallback.call_count == 2


def test_railcore_daily_quota_capping(monkeypatch):
    """Test api_daily_usage tracking in SQLite and capping at MAX_DAILY_LIVE_CALLS."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    monkeypatch.setenv("MAX_DAILY_LIVE_CALLS", "2")
    live_seats.clear_cache()
    live_seats.clear_daily_usage()

    today_ist = live_seats.get_ist_date_str()
    assert live_seats.get_daily_usage() == 0

    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T12:00:00Z",
            "classes": [{"class": "3A", "status": "AVAILABLE", "available_count": 10, "total_fare": 500}],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        # 1. First call -> usage = 1
        live_seats.query_railcore_api("12004", "NDLS", "CNB", "3A", "2026-11-20", "rc_test_key_abc123")
        assert live_seats.get_daily_usage(today_ist) == 1
        assert live_seats.is_daily_quota_available(max_daily=2)

        # 2. Second call -> usage = 2
        live_seats.query_railcore_api("12005", "CNB", "NDLS", "3A", "2026-11-20", "rc_test_key_abc123")
        assert live_seats.get_daily_usage(today_ist) == 2

        # 3. Quota reached! is_daily_quota_available should now be False
        assert not live_seats.is_daily_quota_available(max_daily=2)
        assert not live_seats.can_make_live_call()


def test_railcore_http_errors_fallback_and_no_quota_increment(monkeypatch):
    """HTTP 4xx/5xx errors should fall back cleanly to segments.py and NOT increment daily usage."""
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_test_key_abc123")
    live_seats.clear_cache()
    live_seats.clear_daily_usage()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    http_429 = urllib.error.HTTPError(
        url="https://ir.railcore.tech/v1/availability/seats",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=http_429), \
         patch("live_seats.fallback_segment_seats", return_value=live_seats.LegSeatsResult({"3A": 20}, (), source="simulated_estimate")) as mock_fallback:
        res = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=5,
            leg_depart=375,
            board_dep=375,
            preferred_classes=("3A",),
            allow_live=True,
        )
        assert res == ({"3A": 20}, ())
        assert res.source == "simulated_estimate"
        mock_fallback.assert_called_once()
        # Failed HTTP 429 does not count against daily credit quota
        assert live_seats.get_daily_usage() == 0


def test_railcore_diagnostics_logging(monkeypatch, caplog):
    """Verify logging of RAILCORE_API_KEY detection and exact HTTP status + response details."""
    import logging
    caplog.set_level(logging.INFO)

    # 1. Startup logging when key is unset
    monkeypatch.delenv("RAILCORE_API_KEY", raising=False)
    monkeypatch.delenv("RAILWAY_API_KEY", raising=False)
    live_seats.log_startup_api_key()
    assert any("API key detected at startup: NO" in r.message for r in caplog.records)

    # 2. Startup logging when key is set
    caplog.clear()
    monkeypatch.setenv("RAILCORE_API_KEY", "rc_prod_live_key_987654321")
    live_seats.log_startup_api_key()
    assert any("API key detected at startup: YES" in r.message for r in caplog.records)

    # 3. Query logging of HTTP 200, status string, and checked_at
    caplog.clear()
    mock_payload = {
        "success": True,
        "data": {
            "enquiry_checked_at": "2026-11-20T14:45:00Z",
            "classes": [
                {
                    "class": "3A",
                    "status": "AVAILABLE",
                    "available_count": 30,
                    "total_fare": 820,
                }
            ],
        },
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = live_seats.query_railcore_api(
            train_no="12302",
            from_station="NDLS",
            to_station="CNB",
            class_type="3A",
            travel_date_str="2026-11-20",
            api_key="rc_prod_live_key_987654321",
        )
        assert res is not None
        seats, fare, status_str, checked_at = res
        assert seats == 30
        assert fare == 820
        assert status_str == "AVAILABLE"
        assert checked_at == "2026-11-20T14:45:00Z"

        matching = [r for r in caplog.records if "RailCore API response: HTTP 200" in r.message]
        assert len(matching) >= 1
        assert "status: 'AVAILABLE'" in matching[0].message
        assert "available_count: 30" in matching[0].message


def test_demo_passcode_gating(monkeypatch):
    """Test DEMO_PASSCODE gate in FastAPI endpoints."""
    monkeypatch.setenv("DEMO_PASSCODE", "secret_railcore_pass")
    client = TestClient(app)

    # Mock tools.search_trains to verify allow_live parameter passed
    with patch("train_tools.search_trains", return_value={"success": True, "journeys": []}) as mock_search:
        # 1. Without passcode -> allow_live must be False (fast offline mode)
        res1 = client.get("/api/search?from_station=NDLS&to_station=CNB&travel_date=2026-11-25")
        assert res1.status_code == 200
        assert mock_search.call_args[1]["allow_live"] is False

        # 2. With matching ?unlock= query param -> allow_live must be True
        res2 = client.get("/api/search?from_station=NDLS&to_station=CNB&travel_date=2026-11-25&unlock=secret_railcore_pass")
        assert res2.status_code == 200
        assert mock_search.call_args[1]["allow_live"] is True

        # 3. With matching X-Demo-Passcode header -> allow_live must be True
        res3 = client.get(
            "/api/search?from_station=NDLS&to_station=CNB&travel_date=2026-11-25",
            headers={"X-Demo-Passcode": "secret_railcore_pass"},
        )
        assert res3.status_code == 200
        assert mock_search.call_args[1]["allow_live"] is True

        # 4. With incorrect passcode -> allow_live must be False
        res4 = client.get("/api/search?from_station=NDLS&to_station=CNB&travel_date=2026-11-25&unlock=wrong_passcode")
        assert res4.status_code == 200
        assert mock_search.call_args[1]["allow_live"] is False
