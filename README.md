# Train Booking Agent

A command-line AI agent that finds and books train journeys in plain English. If no direct train has seats, it automatically searches for routes with changes (for example Lucknow → Kanpur → Delhi) and offers them as alternatives.

Built on real Indian Railways timetable data. Seat availability and bookings are **simulated** (see [Limitations](#limitations)).

> Part of my agentic-AI learning roadmap. The previous step was a Google Calendar agent; this one reuses its agent loop (backoff, reflection, context compression) and adds a new problem: time-aware route search.

## Example

```
> Is there a train from Lucknow to Delhi tomorrow evening?
[tool call] search_trains(from_station='Lucknow', to_station='Delhi',
                          travel_date='2026-11-09', earliest='18:00')
-> 5 direct trains, fastest 12233 Lucknow New Delhi AC Express, 23:30 -> 07:08

> /soldout Lucknow > Delhi          (demo helper: every direct train is now sold out)

> book any train any class passenger name is sdf
[tool call] search_trains(...)
-> No direct train with seats. 3 routes with changes found.

   1 (fastest)  12194 Partapgarh Bhopal SF Express   Lucknow -> Kanpur Central
                12309 Rajendra Nagar-New Delhi Rajdhani   Kanpur Central -> Anand Vihar
                departs 22:45, arrives 07:28 next day, layover 2h23m, total 8h43m

> book option 1, 3A
[tool call] book_ticket(legs=[...], travel_class='3A', passenger_name='sdf')
Book 3A for sdf: 12194 LUCKNOW -> KANPUR CENTRAL on 2026-11-09;
                 12309 KANPUR CENTRAL -> ANAND VIHAR TRM on 2026-11-10? [y/N] y
Booked. Your PNR is 3B73798F4E. (Simulated booking.)
```

## How it works

```
raw timetable JSON
      |  build_db.py            clean + reconstruct stop order -> SQLite
      v
data_source.py                  one interface for reading trains and stations
availability.py                 simulated seats (deterministic, can force sold out)
      |
routing.py                      direct search + multi-leg search
options.py                      alternatives, dominance filtering, ranking
      |
train_tools.py                  search / seats / book / cancel / list (with [y/N] gates)
      |
train_agent.py                  LLM loop: tool calling, backoff, reflection, compression
```

The LLM never does the routing. It understands the request, calls tools and explains the results. The route search is plain Python, so it is deterministic and unit-testable.

### Route search

- **Round by round.** Round 1 finds everything reachable on one train, round 2 everything reachable with one change, and so on (up to 3 trains).
- **Time-aware connections.** The next train must leave at least 60 minutes after the previous one arrives (`MIN_LAYOVER`) and no more than 12 hours later (`MAX_LAYOVER`). Connections can roll into the next day.
- **Seats are checked per leg on the date you actually board**, so a second train leaving after midnight is checked against the next day's availability.
- **Dominance filtering.** A journey is dropped if another one is at least as good on departure, arrival and number of changes and strictly better on one of them. This removes pointless routes such as getting off a train one stop early to take a 4-minute hop.
- **Alternatives.** The search is re-run starting just after the earliest departure found, which surfaces later options. Results can be ranked by fastest, earliest arrival or fewest changes.

### Why the tools validate everything

`book_ticket` re-checks that each train really runs between the two stations, that seats are left on that date and that the legs connect, before it asks for confirmation. During testing the model once answered from stale search results and invented a "sold out" claim. The tools refused every bad booking anyway, and the agent now has a prompt rule to re-search whenever availability matters.

## Setup

Requires Python 3.9+ (developed on 3.12).

```bash
git clone https://github.com/ShorezNazeer/train-booking-agent.git
cd train-booking-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Get the timetable data (not included in this repo) and build the database:

```bash
mkdir -p data/raw
git clone https://github.com/datameet/railways.git data/raw/railways
python build_db.py
```

This keeps the trains that stop at several of the 40 busiest stations: about 1,100 trains and 220,000 stops, stored in `data/trains.db`. You can change `TOP_N_STATIONS` and `MIN_MAJOR_STOPS` at the top of `build_db.py`.

Run the agent (it uses [Groq](https://console.groq.com/)):

```bash
export GROQ_API_KEY=your_key_here
python train_agent.py
```

In the chat, `/soldout Lucknow > Delhi` forces every direct train on that route to be sold out, so you can watch the rerouting. It is a demo helper, not a real feature.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

14 unit tests run against a tiny throwaway timetable, so they need neither the real data nor an API key. They cover direct search, wrong direction, sold-out trains, overnight trains, two-train routes, a rejected short layover, a connection into the next day, direct-then-reroute planning, dominance filtering and ranking.

## What is verified, and how

| Part | How it was checked |
|---|---|
| Data cleaning (`build_db.py`) | Run on the real data; stop order and overnight handling inspected on a sample train |
| Direct and multi-leg search | Unit tests, plus runs on real Lucknow → Delhi data |
| Tools (search, seats, book, cancel) | Smoke-tested, including declined confirmations and bad input |
| Agent loop | Run live end to end: search, reroute, multi-leg booking, cancel |
| Retry with backoff | Seen working against a real rate limit during a live run |
| Context compression | Reused from the previous project; **not exercised in live runs of this one** |

## Limitations

- **Seat availability and bookings are simulated.** Seats come from a deterministic hash of train, date and class. Bookings are saved to a local `data/bookings.json`. Nothing touches IRCTC.
- **Every train is assumed to run daily.** The dataset has no running-days field, so a weekly train is treated as daily.
- **The timetable is an older snapshot**, so some trains may no longer exist.
- **Changes only happen at the same station code.** There are no walking transfers between nearby stations.
- **`search_trains` shows base availability** without subtracting your own earlier bookings, so a search can occasionally show a seat that `book_ticket` then refuses.
- **City names are matched with a short hand-picked alias list** (Lucknow, Delhi, Mumbai, Kanpur, Varanasi, Howrah/Kolkata, Chennai); other names fall back to matching station names. Add cities in `CITY_ALIASES` in `data_source.py`.
- No fares, no multiple passengers per booking.

## Project layout

```
build_db.py        clean raw JSON and build data/trains.db
data_source.py     Timetable class: stations, stops, direct trains
availability.py    simulated seat availability
routing.py         Leg, Journey, find_direct, find_routes, plan_trip
options.py         alternatives, dominance filtering, ranking
train_tools.py     tools the agent calls (return success + reason)
train_agent.py     the agent loop and tool schemas
agent_patterns.py  backoff, reflection, compression (from the previous project)
tests/             unit tests
scripts/           manual smoke tests (run from the repo root, e.g. python -m scripts.try_plan)
```

## Data

Timetable data comes from [datameet/railways](https://github.com/datameet/railways), released under CC0.
