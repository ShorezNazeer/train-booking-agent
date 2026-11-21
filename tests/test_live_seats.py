import json
import sqlite3
import time
import urllib.error
import urllib.parse
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import fare_engine
import live_seats
from data_source import DB_PATH, Timetable


@pytest.fixture(autouse=True)
def clean_cache():
    live_seats.clear_cache()
    yield
    live_seats.clear_cache()


def test_live_seats_fallback_when_key_unset(monkeypatch):
    monkeypatch.delenv("RAILWAY_API_KEY", raising=False)
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A", "2A", "SL"]

    with patch("live_seats.fallback_segment_seats", return_value=({"3A": 10}, ())) as mock_fallback:
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
        mock_fallback.assert_called_once()


def test_irctc1_api_request_and_response_available(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    monkeypatch.setenv("RAPIDAPI_HOST", "irctc1.p.rapidapi.com")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["CC", "EC"]

    # Mock irctc1 checkSeatAvailability response with AVAILABLE status
    mock_payload = {
        "status": True,
        "message": "Success",
        "timestamp": 1732100000,
        "data": [
            {
                "date": "20-11-2026",
                "current_status": "AVAILABLE-0042",
                "ticket_fare": 650,
                "total_fare": 695,
            }
        ],
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
        assert mock_urlopen.call_count == 1

        # Verify request headers and query parameters sent to irctc1
        call_args = mock_urlopen.call_args[0]
        req = call_args[0]
        assert req.headers["X-rapidapi-key"] == "rapid_key_test_123"
        assert req.headers["X-rapidapi-host"] == "irctc1.p.rapidapi.com"

        parsed_url = urllib.parse.urlparse(req.full_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        assert params["trainNo"] == ["12004"]
        assert params["fromStationCode"] == ["NDLS"]
        assert params["toStationCode"] == ["CNB"]
        assert params["classType"] == ["CC"]
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


def test_irctc1_sqlite_seat_cache_6hour_ttl(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    mock_payload = {
        "status": True,
        "data": [
            {
                "date": "2026-11-20",
                "current_status": "AVAILABLE-0015",
                "total_fare": 785,
            }
        ],
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

        # Simulate Render restart by clearing memory cache only (leaving SQLite intact)
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
        assert mock_urlopen.call_count == 1  # Still 1, served from SQLite!

        # Simulate 7 hours passing (> 6-hour TTL) in SQLite
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE seat_cache SET cached_at = cached_at - ? WHERE train_no = ?",
                (7 * 3600, "12424"),
            )
            conn.commit()
        live_seats.get_cache().clear()

        # Call after 7 hours should re-fetch from API
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
        assert mock_urlopen.call_count == 2  # Re-fetched because TTL expired!


def test_irctc1_response_waitlist_and_rac(monkeypatch):
    """Test RAC/WL conversion to 0 available seats."""
    assert live_seats.parse_current_status("RAC 5 / WL 12") == 0
    assert live_seats.parse_current_status("RLWL/WL") == 0
    assert live_seats.parse_current_status("GNWL/15") == 0
    assert live_seats.parse_current_status("REGRET") == 0
    assert live_seats.parse_current_status("NOT AVAILABLE") == 0
    assert live_seats.parse_current_status("AVAILABLE-0042") == 42
    assert live_seats.parse_current_status("CURR_AVBL-0008") == 8

    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    mock_payload = {
        "status": True,
        "data": [
            {
                "date": "20-11-2026",
                "current_status": "RLWL/WL 25",
                "total_fare": 785,
            }
        ],
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = live_seats.materialize_leg_seats(
            tt=tt,
            train="12302",
            from_station="NDLS",
            to_station="CNB",
            run_day=date(2026, 11, 20),
            board_seq=0,
            alight_seq=1,
            leg_depart=1010,
            board_dep=1010,
            preferred_classes=("3A",),
        )
        # All candidate classes are waitlisted (0 seats) -> returns None
        assert res is None


def test_irctc1_http_429_too_many_requests_fallback(monkeypatch):
    """On HTTP 429 Too Many Requests, falls back cleanly to segments.py."""
    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    http_429 = urllib.error.HTTPError(
        url="https://irctc1.p.rapidapi.com/api/v1/checkSeatAvailability",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=http_429), \
         patch("live_seats.fallback_segment_seats", return_value=({"3A": 20}, ())) as mock_fallback:
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
        )
        assert res == ({"3A": 20}, ())
        mock_fallback.assert_called_once()


def test_irctc1_http_403_forbidden_fallback(monkeypatch):
    """On HTTP 403 Forbidden, falls back cleanly to segments.py."""
    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["2A"]

    http_403 = urllib.error.HTTPError(
        url="https://irctc1.p.rapidapi.com/api/v1/checkSeatAvailability",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=http_403), \
         patch("live_seats.fallback_segment_seats", return_value=({"2A": 12}, ())) as mock_fallback:
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
            preferred_classes=("2A",),
        )
        assert res == ({"2A": 12}, ())
        mock_fallback.assert_called_once()


def test_quota_guardrails_capping_and_top_candidate_only(monkeypatch):
    """Verify live HTTP calls are capped at 2 uncached checks per search, only on top candidate."""
    monkeypatch.setenv("RAILWAY_API_KEY", "rapid_key_test_123")
    live_seats.clear_cache()
    live_seats.reset_search_quota()

    tt = MagicMock(spec=Timetable)
    tt.train_classes.return_value = ["3A"]

    mock_payload = {
        "status": True,
        "data": [
            {
                "date": "20-11-2026",
                "current_status": "AVAILABLE-0050",
                "total_fare": 650,
            }
        ],
    }
    mock_body = json.dumps(mock_payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_body
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen, \
         patch("live_seats.fallback_segment_seats", return_value=({"3A": 15}, ())) as mock_fallback:

        # 1. Non-top candidate journey (allow_live=False) must NOT make uncached HTTP calls
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
        assert mock_urlopen.call_count == 0  # No HTTP call for non-top journey!
        assert mock_fallback.call_count == 1

        # 2. Top candidate journey leg 1 (uncached call 1)
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

        # 3. Top candidate journey leg 2 (uncached call 2)
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
        assert res_top2 == ({"3A": 50}, ())
        assert mock_urlopen.call_count == 2
        assert live_seats.get_uncached_call_count() == 2

        # 4. Third uncached check in the same search must be blocked by the quota cap of 2
        assert not live_seats.can_make_live_call()
        res_top3 = live_seats.materialize_leg_seats(
            tt=tt,
            train="12004",
            from_station="LKO",
            to_station="GKP",
            run_day=date(2026, 11, 20),
            board_seq=8,
            alight_seq=12,
            leg_depart=800,
            board_dep=800,
            preferred_classes=("3A",),
            allow_live=True,
        )
        assert res_top3 == ({"3A": 15}, ())
        assert mock_urlopen.call_count == 2  # Capped at 2! No 3rd HTTP call!
        assert mock_fallback.call_count == 2  # Fell back cleanly to segments.py
