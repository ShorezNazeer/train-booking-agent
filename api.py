"""FastAPI backend for Train Booking Engine."""
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from groq import Groq
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import availability as av
import segments as seg
import train_agent
import train_tools as tools
from agent_patterns import call_groq_with_backoff, compress_if_needed, reflect_on_result
from data_source import CITY_ALIASES

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Train Booking Engine API",
    description="Modern Train Booking Engine with AI Assistant, Segmented Class Changes, and Multi-Train Routing",
    version="1.0.0",
)

# Enable CORS for local testing and decoupled frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global exception handler returning {"detail": str(exc)} for unhandled server errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    if isinstance(exc, RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )


# ---------- Pydantic Schemas ----------
class LegSchema(BaseModel):
    train: str
    from_code: str
    to_code: str
    date: str
    travel_class: Optional[str] = None


class BookRequest(BaseModel):
    legs: List[LegSchema]
    passenger_name: str
    travel_class: Optional[str] = None


class CancelRequest(BaseModel):
    pnr: str


class SearchRequest(BaseModel):
    from_station: str
    to_station: str
    travel_date: str
    earliest: str = "00:00"
    sort: str = "fastest"
    limit: int = 5


class CheckSeatsRequest(BaseModel):
    train_number: str
    travel_date: str
    from_station: Optional[str] = None
    to_station: Optional[str] = None
    travel_class: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[dict]] = None


# ---------- Autocomplete Helper ----------
@app.get("/api/stations")
def get_stations(q: Optional[str] = Query(None, description="Search query")):
    tt = tools._timetable()
    limit = 12
    if not q or not q.strip():
        # Top major hubs
        popular = ["delhi", "mumbai", "lucknow", "kanpur", "varanasi", "howrah", "chennai"]
        results = []
        for city in popular:
            codes = CITY_ALIASES.get(city, [])
            results.append({
                "code": codes[0] if codes else city.upper(),
                "name": f"{city.title()} (All Stations: {', '.join(codes)})",
                "is_city": True,
                "query_val": city.title(),
            })
        return results

    query = q.strip().lower()
    matches = []
    seen_codes = set()

    # 1. City aliases match
    for city, codes in CITY_ALIASES.items():
        if city.startswith(query) or query in city:
            c0 = codes[0] if codes else city.upper()
            seen_codes.add(c0)
            matches.append({
                "code": c0,
                "name": f"{city.title()} (All Stations: {', '.join(codes)})",
                "is_city": True,
                "query_val": city.title(),
            })

    # 2. Station code prefix or match
    q_upper = query.upper()
    for code, name in tt._stations.items():
        if code.startswith(q_upper) and code not in seen_codes:
            seen_codes.add(code)
            matches.append({
                "code": code,
                "name": f"{name} ({code})",
                "is_city": False,
                "query_val": code,
            })
            if len(matches) >= limit:
                break

    # 3. Station name match
    if len(matches) < limit:
        for code, name in tt._stations.items():
            if not name or code in seen_codes:
                continue
            n_low = name.lower()
            if n_low.startswith(query) or f" {query}" in n_low or query in n_low:
                seen_codes.add(code)
                matches.append({
                    "code": code,
                    "name": f"{name} ({code})",
                    "is_city": False,
                    "query_val": code,
                })
                if len(matches) >= limit:
                    break

    return matches


# ---------- Search Endpoint ----------
@app.get("/api/search")
def search_trains_get(
    from_station: str = Query(..., description="Origin city/station"),
    to_station: str = Query(..., description="Destination city/station"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
    earliest: str = Query("00:00", description="Earliest time HH:MM"),
    sort: str = Query("fastest", description="fastest, earliest, or fewest_changes"),
    limit: int = Query(5, ge=1, le=20),
):
    res = tools.search_trains(
        from_station=from_station,
        to_station=to_station,
        travel_date=travel_date,
        earliest=earliest,
        sort=sort,
        limit=limit,
    )
    return res


@app.post("/api/search")
def search_trains_post(req: SearchRequest):
    return tools.search_trains(
        from_station=req.from_station,
        to_station=req.to_station,
        travel_date=req.travel_date,
        earliest=req.earliest,
        sort=req.sort,
        limit=req.limit,
    )


# ---------- Check Seats Endpoint ----------
@app.get("/api/check-seats")
def check_seats_get(
    train_number: str = Query(..., description="Train number"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
    from_station: Optional[str] = Query(None),
    to_station: Optional[str] = Query(None),
    travel_class: Optional[str] = Query(None),
):
    return tools.check_seats(
        train_number=train_number,
        travel_date=travel_date,
        from_station=from_station,
        to_station=to_station,
        travel_class=travel_class,
    )


@app.post("/api/check-seats")
def check_seats_post(req: CheckSeatsRequest):
    return tools.check_seats(
        train_number=req.train_number,
        travel_date=req.travel_date,
        from_station=req.from_station,
        to_station=req.to_station,
        travel_class=req.travel_class,
    )


# ---------- Book Ticket Endpoint ----------
@app.post("/api/book")
def book_ticket(req: BookRequest):
    # Pass confirm=lambda _: True since confirmation happens in a UI modal
    legs = [l.model_dump(exclude_none=True) for l in req.legs]
    res = tools.book_ticket(
        legs=legs,
        travel_class=req.travel_class,
        passenger_name=req.passenger_name,
        confirm=lambda _: True,
    )
    return res


# ---------- Bookings Endpoint ----------
@app.get("/api/bookings")
def get_bookings():
    return tools.list_bookings()


# ---------- Cancel Booking Endpoint ----------
@app.post("/api/cancel")
def cancel_ticket_post(req: CancelRequest):
    return tools.cancel_ticket(pnr=req.pnr, confirm=lambda _: True)


@app.delete("/api/bookings/{pnr}")
def cancel_ticket_delete(pnr: str):
    return tools.cancel_ticket(pnr=pnr, confirm=lambda _: True)


# ---------- One-Click Demo Endpoints ----------
@app.get("/api/demo/direct")
@app.post("/api/demo/direct")
def demo_direct():
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "success": True,
        "type": "direct",
        "from_station": "Delhi",
        "to_station": "Kanpur",
        "travel_date": tomorrow,
        "description": "Standard direct trains with available single-class seats (SL, 3A, 2A).",
    }


@app.get("/api/demo/split")
@app.post("/api/demo/split")
def demo_split():
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    train_agent.force_split_demo("Lucknow", "Delhi")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "success": True,
        "type": "split",
        "from_station": "Lucknow",
        "to_station": "Delhi",
        "travel_date": tomorrow,
        "description": "Forces SL sold out on 2nd half and 3A on 1st half. Mid-route class/coach change is required.",
    }


@app.get("/api/demo/soldout")
@app.post("/api/demo/soldout")
def demo_soldout():
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    train_agent.force_sold_out("Lucknow", "Delhi")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "success": True,
        "type": "soldout",
        "from_station": "Lucknow",
        "to_station": "Delhi",
        "travel_date": tomorrow,
        "description": "Forces all direct trains sold out. Reroutes through multi-train transfers with layovers.",
    }


@app.get("/api/demo/reset")
@app.post("/api/demo/reset")
def demo_reset():
    av.FORCED_SOLD_OUT.clear()
    seg.FORCED_SEGMENT_SOLD_OUT.clear()
    return {"success": True, "description": "All demo simulation overrides cleared."}


# ---------- Chat Assistant Endpoint ----------
WEB_TOOL_IMPL = {
    "search_trains": tools.search_trains,
    "check_seats": tools.check_seats,
    "book_ticket": lambda **kwargs: tools.book_ticket(**kwargs, confirm=lambda _: True),
    "cancel_ticket": lambda **kwargs: tools.cancel_ticket(**kwargs, confirm=lambda _: True),
    "list_bookings": tools.list_bookings,
}


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "reply": "GROQ_API_KEY is not configured on the server. Please set the GROQ_API_KEY environment variable to use the AI assistant.",
                "history": req.history or [],
                "tool_calls": [],
            },
        )

    try:
        groq_client = Groq(api_key=groq_api_key)
    except Exception as err:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "reply": f"Failed to initialize Groq client: {err}",
                "history": req.history or [],
                "tool_calls": [],
            },
        )

    # Initialize or refresh messages
    messages = list(req.history or [])
    system_prompt = train_agent.build_system_prompt()
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": system_prompt})
    else:
        messages[0] = {"role": "system", "content": system_prompt}

    messages.append({"role": "user", "content": req.message})

    turn_tool_calls = []

    try:
        for iteration in range(train_agent.MAX_ITERATIONS):
            messages = compress_if_needed(messages, groq_client, train_agent.MODEL)
            response = call_groq_with_backoff(
                groq_client,
                model=train_agent.MODEL,
                messages=messages,
                tools=train_agent.TOOL_SCHEMAS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            # Append cleanly to messages
            asst_dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                asst_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(asst_dict)

            if not msg.tool_calls:
                break

            for call in msg.tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments or "{}")
                impl = WEB_TOOL_IMPL.get(name)
                if impl is None:
                    res_obj = {"success": False, "reason": f"unknown tool '{name}'"}
                else:
                    try:
                        res_obj = impl(**args)
                    except TypeError as err:
                        res_obj = {"success": False, "reason": f"bad arguments: {err}"}

                reflection = reflect_on_result(name, res_obj)
                turn_tool_calls.append({
                    "name": name,
                    "args": args,
                    "result": res_obj,
                    "summary": reflection,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": name,
                    "content": f"{reflection}\n\n{json.dumps(res_obj)}",
                })
        else:
            asst_dict = {
                "role": "assistant",
                "content": "I apologize, but I reached the maximum step limit before finishing.",
            }
            messages.append(asst_dict)

        final_reply = ""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                final_reply = m["content"]
                break

        return {
            "success": True,
            "reply": final_reply or "Done.",
            "history": messages,
            "tool_calls": turn_tool_calls,
        }

    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "reply": f"An error occurred while communicating with the agent: {str(exc)}",
                "history": messages,
                "tool_calls": turn_tool_calls,
            },
        )


# ---------- Static Files and Frontend Serving ----------
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Frontend index.html is being prepared."}


# Mount root static files fallback AFTER all /api/* routes
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
