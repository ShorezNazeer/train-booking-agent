"""Step 11: the train booking agent (same loop as the Step 6 calendar agent)."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from groq import Groq

import availability as av
import train_tools as tools
from agent_patterns import call_groq_with_backoff, compress_if_needed, reflect_on_result

MODEL = "openai/gpt-oss-120b"
MAX_ITERATIONS = 8
TZ = "Asia/Kolkata"

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

TOOL_IMPL = {
    "search_trains": tools.search_trains,
    "check_seats": tools.check_seats,
    "book_ticket": tools.book_ticket,
    "cancel_ticket": tools.cancel_ticket,
    "list_bookings": tools.list_bookings,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_trains",
            "description": (
                "Search trains between two places for a date. Returns direct trains "
                "with seats if any exist; otherwise it automatically returns the best "
                "alternative routes with changes (e.g. A -> C -> B). Each journey lists "
                "its legs, times, layovers and seats."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_station": {"type": "string", "description": "City or station name or code, e.g. Lucknow"},
                    "to_station": {"type": "string", "description": "City or station name or code, e.g. Delhi"},
                    "travel_date": {"type": "string", "description": "YYYY-MM-DD, the date of departure"},
                    "earliest": {"type": "string", "description": "Earliest departure time, 24-hour HH:MM. Default 00:00."},
                    "sort": {
                        "type": "string",
                        "enum": ["fastest", "earliest", "fewest_changes"],
                        "description": "How to rank the options. Default fastest.",
                    },
                },
                "required": ["from_station", "to_station", "travel_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_seats",
            "description": "Check seats left on one train for a date, per class (SL, 3A, 2A).",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_number": {"type": "string"},
                    "travel_date": {"type": "string", "description": "YYYY-MM-DD, the date you board this train"},
                    "travel_class": {"type": "string", "enum": ["SL", "3A", "2A"]},
                },
                "required": ["train_number", "travel_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_ticket",
            "description": (
                "Book one or more connected legs for one passenger. Requires user "
                "confirmation. Pass the legs exactly as returned by search_trains."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "legs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "train": {"type": "string"},
                                "from_code": {"type": "string"},
                                "to_code": {"type": "string"},
                                "date": {"type": "string", "description": "YYYY-MM-DD, the date you board this train"},
                            },
                            "required": ["train", "from_code", "to_code", "date"],
                        },
                    },
                    "travel_class": {"type": "string", "enum": ["SL", "3A", "2A"]},
                    "passenger_name": {"type": "string"},
                },
                "required": ["legs", "travel_class", "passenger_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_ticket",
            "description": "Cancel a booking by its PNR. Requires user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}},
                "required": ["pnr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_bookings",
            "description": "List the user's active bookings, including their PNRs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def build_system_prompt():
    now = datetime.now(ZoneInfo(TZ))
    return f"""You are a train booking assistant for Indian Railways routes.

Current date and time: {now:%A, %d %B %Y, %H:%M} (timezone: {TZ}).
Use this to resolve words like "today", "tomorrow" and "next Friday".
Tools take dates as YYYY-MM-DD and times as 24-hour HH:MM.

Rules:
1. For every travel question, call search_trains, even if you searched the same
   route earlier in this conversation. Seat availability changes, so never answer
   from earlier results and never list trains or seats from memory. If the user
   gives a time preference ("evening", "after 6pm"), pass it as `earliest`.
2. If search_trains returns routes with changes (transfers > 0), tell the user
   there is no direct train with seats and present these as alternatives. For each
   option give: departure and arrival time (and date if it is the next day), total
   duration, the trains in order, the change stations and the layover lengths.
   Present at most 3 options, and mention which is fastest.
3. Never book until the user has chosen an option, a class (SL, 3A or 2A) and given
   the passenger's name. Ask for whatever is missing. Do not invent a name.
4. When booking, pass the legs exactly as search_trains returned them (train,
   from_code, to_code, date).
5. book_ticket and cancel_ticket ask the user for confirmation. If the user
   declines, do not retry; ask what to change instead.
6. To cancel, you need the PNR. If the user does not have it, call list_bookings.
7. If a tool returns success false, tell the user plainly what failed and why.
   If book_ticket fails because seats are gone, call search_trains again for the
   same trip and present only what that new search returns, including routes with
   changes. Never say seats are sold out or booked unless a tool result said so.
8. Bookings here are simulated and the timetable is an older snapshot that assumes
   every train runs daily. Mention this briefly after the first booking.
9. Do not call tools for simple chat. When a task is done, give a short summary.
"""


def _error(message):
    # Both keys, so the agent's own errors look like the tools' errors
    return {"success": False, "reason": message, "detail": message}


def force_sold_out(from_text, to_text):
    """Demo helper: make every direct train on a route sold out."""
    tt = tools._timetable()
    src = tt.resolve_station(from_text)
    dst = tt.resolve_station(to_text)
    if not src or not dst:
        print("[demo] I couldn't find one of those stations.")
        return
    trains = {r["train"] for r in tt.direct_trains(src, dst)}
    for train in trains:
        av.FORCED_SOLD_OUT.add((train, None))
    print(f"[demo] {len(trains)} direct trains from {from_text} to {to_text} are now sold out.")


def run_agent(messages: list, task: str) -> list:
    # Refresh the date/time on every task so "tomorrow" is always correct
    messages[0] = {"role": "system", "content": build_system_prompt()}
    messages.append({"role": "user", "content": task})

    for iteration in range(MAX_ITERATIONS):
        messages = compress_if_needed(messages, client, MODEL)
        response = call_groq_with_backoff(
            client, model=MODEL, messages=messages, tools=TOOL_SCHEMAS, tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if msg.content:
            print(f"\n--- agent (iteration {iteration + 1}) ---")
            print(msg.content)

        if not msg.tool_calls:
            break

        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            print(f"\n[tool call] {name}({args})")

            impl = TOOL_IMPL.get(name)
            if impl is None:
                result = _error(f"unknown tool '{name}'")
            else:
                try:
                    result = impl(**args)
                except TypeError as err:
                    result = _error(f"bad arguments: {err}")

            print(f"[tool result] {str(result)[:300]}")
            reflection = reflect_on_result(name, result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": f"{reflection}\n\n{json.dumps(result)}",
            })
    else:
        print("\n[stopped: reached MAX_ITERATIONS without the agent finishing]")

    return messages


if __name__ == "__main__":
    messages = [{"role": "system", "content": build_system_prompt()}]
    print("Train booking assistant. Type 'quit' to exit.")
    print("Demo helper: /soldout Lucknow > Delhi  (forces direct trains sold out)")
    while True:
        task = input("\nWhere would you like to travel? (or 'quit')\n> ")
        text = task.strip()
        if text.lower() in ("quit", "exit"):
            break
        if not text:
            continue
        if text.lower().startswith("/soldout"):
            parts = text[len("/soldout"):].split(">")
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                force_sold_out(parts[0].strip(), parts[1].strip())
                messages.append({"role": "user", "content": "[notice] Seat availability has changed since your last search. Earlier search results are out of date, so search again before answering."})
            else:
                print("[demo] usage: /soldout Lucknow > Delhi")
            continue
        messages = run_agent(messages, task)
