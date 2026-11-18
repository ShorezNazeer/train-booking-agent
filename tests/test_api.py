from datetime import date, timedelta
from fastapi.testclient import TestClient
from api import app

client = TestClient(app)
tomorrow = (date.today() + timedelta(days=1)).isoformat()


def test_index_and_static_files():
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "RailEngine AI" in res.text

    css_res = client.get("/static/style.css")
    assert css_res.status_code == 200
    assert "segmented-bar" in css_res.text

    js_res = client.get("/static/app.js")
    assert js_res.status_code == 200
    assert "executeSearch" in js_res.text


def test_stations_autocomplete():
    res = client.get("/api/stations?q=delhi")
    assert res.status_code == 200
    stations = res.json()
    assert len(stations) > 0
    assert any("Delhi" in s["name"] or s["code"] == "NDLS" for s in stations)

    empty_res = client.get("/api/stations")
    assert empty_res.status_code == 200
    assert len(empty_res.json()) > 0


def test_search_trains_get_and_post():
    get_res = client.get(
        f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}&earliest=00:00&sort=fastest"
    )
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["success"] is True
    assert len(data.get("journeys", [])) > 0

    post_res = client.post(
        "/api/search",
        json={
            "from_station": "Delhi",
            "to_station": "Kanpur",
            "travel_date": tomorrow,
            "earliest": "00:00",
            "sort": "fastest",
            "limit": 5,
        },
    )
    assert post_res.status_code == 200
    assert post_res.json()["success"] is True


def test_check_seats():
    search_res = client.get(
        f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}"
    )
    assert search_res.status_code == 200
    journeys = search_res.json()["journeys"]
    assert len(journeys) > 0
    first_leg = journeys[0]["legs"][0]

    check_res = client.get(
        f"/api/check-seats?train_number={first_leg['train']}&travel_date={first_leg['date']}&from_station={first_leg['from_code']}&to_station={first_leg['to_code']}"
    )
    assert check_res.status_code == 200
    data = check_res.json()
    assert data["success"] is True
    assert "seats" in data
    assert "stretches" in data


def test_booking_and_cancellation_flow():
    search_res = client.get(
        f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}"
    )
    journeys = search_res.json()["journeys"]
    j = journeys[0]
    legs = [
        {
            "train": leg["train"],
            "from_code": leg["from_code"],
            "to_code": leg["to_code"],
            "date": leg["date"],
            "travel_class": "3A",
        }
        for leg in j["legs"]
    ]

    # Test booking
    book_res = client.post(
        "/api/book",
        json={
            "legs": legs,
            "passenger_name": "Pytest Traveler",
            "travel_class": "3A",
        },
    )
    assert book_res.status_code == 200
    b_data = book_res.json()
    if b_data.get("success"):
        pnr = b_data["booking"]["pnr"]
        assert len(pnr) > 0

        # Check in active bookings
        list_res = client.get("/api/bookings")
        assert list_res.status_code == 200
        active_pnrs = [b["pnr"] for b in list_res.json()["bookings"]]
        assert pnr in active_pnrs

        # Cancel the ticket
        cancel_res = client.post("/api/cancel", json={"pnr": pnr})
        assert cancel_res.status_code == 200
        assert cancel_res.json()["success"] is True

        # Check it is no longer in active bookings
        list_res2 = client.get("/api/bookings")
        active_pnrs2 = [b["pnr"] for b in list_res2.json()["bookings"]]
        assert pnr not in active_pnrs2


def test_demo_endpoints():
    # Test POST
    r1 = client.post("/api/demo/direct")
    assert r1.status_code == 200
    assert r1.json()["type"] == "direct"

    r2 = client.post("/api/demo/split")
    assert r2.status_code == 200
    assert r2.json()["type"] == "split"

    r3 = client.post("/api/demo/soldout")
    assert r3.status_code == 200
    assert r3.json()["type"] == "soldout"

    r4 = client.post("/api/demo/reset")
    assert r4.status_code == 200
    assert r4.json()["success"] is True

    # Test GET
    g1 = client.get("/api/demo/direct")
    assert g1.status_code == 200
    assert g1.json()["type"] == "direct"

    g2 = client.get("/api/demo/split")
    assert g2.status_code == 200
    assert g2.json()["type"] == "split"

    g3 = client.get("/api/demo/soldout")
    assert g3.status_code == 200
    assert g3.json()["type"] == "soldout"

    g4 = client.get("/api/demo/reset")
    assert g4.status_code == 200
    assert g4.json()["success"] is True


def test_global_exception_handler(monkeypatch):
    from fastapi.testclient import TestClient
    from api import app
    import train_tools as tools

    safe_client = TestClient(app, raise_server_exceptions=False)

    # Trigger a 404
    r_404 = safe_client.get("/api/nonexistent-endpoint")
    assert r_404.status_code == 404
    assert "detail" in r_404.json()

    # Trigger a 422
    r_422 = safe_client.post("/api/book", json={"bad_field": 123})
    assert r_422.status_code == 422
    assert "detail" in r_422.json()

    # Trigger an unhandled error in a real route to verify 500 JSON response format
    def mock_fail(**kwargs):
        raise RuntimeError("Simulated database crash")

    monkeypatch.setattr(tools, "search_trains", mock_fail)

    r_500 = safe_client.get(f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}")
    assert r_500.status_code == 500
    assert "application/json" in r_500.headers.get("content-type", "")
    assert r_500.json() == {"detail": "Simulated database crash"}


def test_chat_assistant():
    chat_res = client.post(
        "/api/chat",
        json={"message": "List active bookings", "history": []},
    )
    assert chat_res.status_code == 200
    data = chat_res.json()
    assert data["success"] is True
    assert "reply" in data
    assert len(data["reply"]) > 0
    assert isinstance(data["tool_calls"], list)


def test_session_booking_isolation():
    search_res = client.get(
        f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}"
    )
    journeys = search_res.json()["journeys"]
    j = next((x for x in journeys if x["legs"][0].get("seats")), journeys[0])
    first_leg = j["legs"][0]
    chosen_cls = next(iter(first_leg["seats"])) if first_leg.get("seats") else "3A"

    legs = [
        {
            "train": first_leg["train"],
            "from_code": first_leg["from_code"],
            "to_code": first_leg["to_code"],
            "date": first_leg["date"],
            "travel_class": chosen_cls,
        }
    ]

    session_a = "user-sess-aaa"
    session_b = "user-sess-bbb"

    # User A books
    book_res_a = client.post(
        "/api/book",
        json={
            "legs": legs,
            "passenger_name": "Traveler A",
            "travel_class": chosen_cls,
        },
        headers={"X-Session-ID": session_a},
    )
    assert book_res_a.status_code == 200
    pnr_a = book_res_a.json()["booking"]["pnr"]

    # User A sees their booking
    list_a = client.get("/api/bookings", headers={"X-Session-ID": session_a})
    pnrs_a = [b["pnr"] for b in list_a.json()["bookings"]]
    assert pnr_a in pnrs_a

    # User B does NOT see User A's booking
    list_b = client.get("/api/bookings", headers={"X-Session-ID": session_b})
    pnrs_b = [b["pnr"] for b in list_b.json()["bookings"]]
    assert pnr_a not in pnrs_b

    # User B cannot cancel User A's booking
    cancel_b = client.post("/api/cancel", json={"pnr": pnr_a}, headers={"X-Session-ID": session_b})
    assert cancel_b.status_code == 200
    assert cancel_b.json()["success"] is False

    # User A cancels their booking
    cancel_a = client.post("/api/cancel", json={"pnr": pnr_a}, headers={"X-Session-ID": session_a})
    assert cancel_a.status_code == 200
    assert cancel_a.json()["success"] is True

    # User A no longer sees it
    list_a2 = client.get("/api/bookings", headers={"X-Session-ID": session_a})
    pnrs_a2 = [b["pnr"] for b in list_a2.json()["bookings"]]
    assert pnr_a not in pnrs_a2


def test_rate_limiting_chat(monkeypatch):
    limiter = app.state.limiter
    limiter.reset()

    # Mock groq call to prevent actual network requests
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    # Rate limit on /api/chat is 10/minute
    got_429 = False
    for _ in range(15):
        res = client.post("/api/chat", json={"message": "ping", "history": []})
        if res.status_code == 429:
            got_429 = True
            break
    assert got_429, "Expected 429 Too Many Requests after exceeding 10 req/min on /api/chat"
    limiter.reset()


def test_rate_limiting_search():
    limiter = app.state.limiter
    limiter.reset()

    # Rate limit on /api/search is 30/minute
    got_429 = False
    for _ in range(35):
        res = client.get(f"/api/search?from_station=Delhi&to_station=Kanpur&travel_date={tomorrow}")
        if res.status_code == 429:
            got_429 = True
            break
    assert got_429, "Expected 429 Too Many Requests after exceeding 30 req/min on /api/search"
    limiter.reset()


def test_ndls_cnb_current_trains_and_chronological_stops():
    from data_source import Timetable

    tt = Timetable()

    # 1. Verify 12004 Shatabdi has exact 9 stops in order
    s12004 = tt.stops_of("12004")
    assert len(s12004) == 9, f"Expected 9 stops for 12004 Shatabdi, got {len(s12004)}"
    expected_codes_12004 = ["NDLS", "GZB", "ALJN", "TDL", "ETW", "PHD", "CNB", "ON", "LJN"]
    assert [s.station for s in s12004] == expected_codes_12004
    assert all(s.seq == i for i, s in enumerate(s12004))
    assert s12004[0].arr is None and s12004[0].dep == 370  # 06:10
    assert s12004[6].station == "CNB" and s12004[6].arr == 683 and s12004[6].dep == 688  # 11:23 -> 11:28
    assert s12004[-1].dep is None and s12004[-1].arr == 780  # 13:00

    # 2. Verify 12302 Rajdhani has exact 9 stops in order
    s12302 = tt.stops_of("12302")
    assert len(s12302) == 9, f"Expected 9 stops for 12302 Rajdhani, got {len(s12302)}"
    expected_codes_12302 = ["NDLS", "CNB", "PRYJ", "DDU", "GAYA", "PNME", "DHN", "ASN", "HWH"]
    assert [s.station for s in s12302] == expected_codes_12302
    assert all(s.seq == i for i, s in enumerate(s12302))

    # 3. Verify 22436 Vande Bharat has exact 4 stops in order
    s22436 = tt.stops_of("22436")
    assert len(s22436) == 4, f"Expected 4 stops for 22436 Vande Bharat, got {len(s22436)}"
    expected_codes_22436 = ["NDLS", "CNB", "PRYJ", "BSB"]
    assert [s.station for s in s22436] == expected_codes_22436
    assert all(s.seq == i for i, s in enumerate(s22436))

    # 4. Search NDLS to CNB via API and verify real trains are returned
    res = client.get(
        f"/api/search?from_station=NDLS&to_station=CNB&travel_date={tomorrow}&earliest=00:00&sort=fastest&limit=20"
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    found_trains = {j["legs"][0]["train"] for j in data["journeys"]}
    assert "22436" in found_trains or "12004" in found_trains

