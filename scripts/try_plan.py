import time
from datetime import date

import availability as av
from data_source import Timetable
from routing import fmt_time, plan_trip

t = Timetable()
d = date(2026, 11, 13)
src = t.resolve_station("Lucknow")
dst = t.resolve_station("Delhi")

# Force every direct Lucknow -> Delhi train sold out
for r in t.direct_trains(src, dst):
    av.FORCED_SOLD_OUT.add((r["train"], None))

start = time.time()
journeys = plan_trip(t, "Lucknow", "Delhi", d)
print(f"{len(journeys)} journeys found in {time.time() - start:.1f}s")

for j in journeys:
    print(f"--- {j.transfers} change(s), {j.duration // 60}h{j.duration % 60:02d}m total")
    for leg in j.legs:
        print(
            "  ", leg.train, leg.name, "|",
            t.station_name(leg.from_station), fmt_time(leg.depart), "->",
            t.station_name(leg.to_station), fmt_time(leg.arrive),
        )
    if j.transfers:
        print("   layovers (min):", j.layovers())
