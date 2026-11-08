import time
from datetime import date

import availability as av
from data_source import Timetable
from options import SORT_KEYS, plan_options
from routing import fmt_time

t = Timetable()
d = date(2026, 11, 13)


def show(journeys):
    for j in journeys:
        print(f"--- {j.transfers} change(s), leaves {fmt_time(j.depart)}, "
              f"arrives {fmt_time(j.arrive)}, {j.duration // 60}h{j.duration % 60:02d}m")
        for leg in j.legs:
            print("   ", leg.train, leg.name, "|",
                  t.station_name(leg.from_station), fmt_time(leg.depart), "->",
                  t.station_name(leg.to_station), fmt_time(leg.arrive))


# Force every direct Lucknow -> Delhi train sold out
for r in t.direct_trains(t.resolve_station("Lucknow"), t.resolve_station("Delhi")):
    av.FORCED_SOLD_OUT.add((r["train"], None))

start = time.time()
options = plan_options(t, "Lucknow", "Delhi", d, limit=10)
print(f"{len(options)} options in {time.time() - start:.1f}s\n")

print("FASTEST FIRST")
show(options)

print("\nFEWEST CHANGES")
show(sorted(options, key=SORT_KEYS["fewest_changes"])[:1])
