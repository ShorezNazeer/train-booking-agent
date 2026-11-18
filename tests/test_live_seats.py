import time
from datetime import date
from unittest.mock import MagicMock, patch

import live_seats
from data_source import Timetable


def test_live_seats_fallback_when_key_unset(monkeypatch):
    monkeypatch.delenv("RAILWAY_API_KEY", raising=False)
    live_seats.clear_cache()

    # Mock timetable and stops
    tt = MagicMock(spec=Timetable)
    # Call materialize_leg_seats
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


def test_live_seats_api_call_and_15min_ttl_cache(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_KEY", "test_key_123")
    monkeypatch.setenv("RAILWAY_API_URL", "https://api.railway.test/v1")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)
    mock_payload = b'{"seats": {"1A": 4, "2A": 8, "3A": 15, "SL": 30}}'

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_payload
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res1 = live_seats.materialize_leg_seats(
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
        assert res1 is not None
        seats, split = res1
        assert seats == {"1A": 4, "2A": 8, "3A": 15, "SL": 30}
        assert split == ()
        assert mock_urlopen.call_count == 1

        # Second call immediately should hit cache (0 additional urlopen calls)
        res2 = live_seats.materialize_leg_seats(
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
        assert res2 == res1
        assert mock_urlopen.call_count == 1  # Still 1, served from cache!

        # Verify cache key structure
        cache = live_seats.get_cache()
        expected_key = ("12004", "NDLS", "CNB", "2026-11-20")
        assert expected_key in cache

        # Simulate 16 minutes passing (expired TTL)
        ts, data = cache[expected_key]
        cache[expected_key] = (ts - (16 * 60), data)

        res3 = live_seats.materialize_leg_seats(
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
        assert res3 == res1
        assert mock_urlopen.call_count == 2  # Refreshed after TTL expired!


def test_live_seats_api_error_fallback(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_KEY", "test_key_123")
    live_seats.clear_cache()

    tt = MagicMock(spec=Timetable)

    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")), \
         patch("live_seats.fallback_segment_seats", return_value=({"2A": 5}, ())) as mock_fallback:
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
        assert res == ({"2A": 5}, ())
        mock_fallback.assert_called_once()
