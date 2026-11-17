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
