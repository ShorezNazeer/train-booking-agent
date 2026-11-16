from pathlib import Path

import train_tools as tools

tools.BOOKINGS_PATH = Path("/tmp/test_bookings.json")
if tools.BOOKINGS_PATH.exists():
    tools.BOOKINGS_PATH.unlink()

yes = lambda prompt: (print("  [auto-confirm]", prompt), True)[1]
no = lambda prompt: False

r = tools.search_trains("Lucknow", "Delhi", "2026-11-13", earliest="18:00")
print("SEARCH:", r["success"], "|", r["reason"])
j = r["journeys"][0]
print("  first option:", j["departs"], "->", j["arrives"], [l["train"] for l in j["legs"]])

print("SEATS:", tools.check_seats(j["legs"][0]["train"], "2026-11-13")["seats"])

legs = [{k: l[k] for k in ("train", "from_code", "to_code", "date")} for l in j["legs"]]

print("DECLINED:", tools.book_ticket(legs, "3A", "Test Person", confirm=no)["reason"])
b = tools.book_ticket(legs, "3A", "Test Person", confirm=yes)
print("BOOKED:", b["success"], "|", b["reason"])
print("LIST:", tools.list_bookings()["reason"])
c = tools.cancel_ticket(b["booking"]["pnr"], confirm=yes)
print("CANCELLED:", c["success"], "|", c["reason"])
print("LIST:", tools.list_bookings()["reason"])

print("BAD DATE:", tools.search_trains("Lucknow", "Delhi", "13-11-2026")["reason"])
print("BAD CLASS:", tools.book_ticket(legs, "XX", "Test Person")["reason"])
print("BAD STATION:", tools.search_trains("Atlantis", "Delhi", "2026-11-13")["reason"])
