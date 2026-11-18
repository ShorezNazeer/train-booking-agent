"""fare_engine.py — Indian Railways fare calculation engine.

Computes realistic Indian Railways INR fares per stretch using:
- Track distance (derived from station coordinates via Haversine with rail track winding factor).
- Telescopic distance slab rates for 1A, 2A, 3A, 3E, CC, EC, SL, 2S.
- Reservation charges per class.
- Superfast surcharges.
- 5% GST on AC classes (1A, 2A, 3A, 3E, CC, EC); non-AC (SL, 2S) are exempt.
- Indian Railways rounding to the nearest multiple of ₹5.
"""
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RAW_DIR = Path(__file__).resolve().parent / "raw_data"
STATIONS_JSON = RAW_DIR / "stations.json"

# Fixed coordinates for major stations/junctions for guaranteed zero-IO fallback
PRIMARY_STATION_COORDS: Dict[str, Tuple[float, float]] = {
    "NDLS": (28.6423, 77.2200),
    "DLI": (28.6606, 77.2294),
    "NZM": (28.5886, 77.2534),
    "ANVT": (28.6502, 77.3152),
    "CNB": (26.4542, 80.3510),
    "GOY": (26.4468, 80.3168),
    "LKO": (26.8307, 80.9253),
    "LJN": (26.8322, 80.9202),
    "ALJN": (27.8974, 78.0880),
    "TDL": (27.2064, 78.2435),
    "ETW": (26.7769, 79.0270),
    "PHD": (26.5614, 79.5202),
    "ON": (26.5458, 80.4907),
    "PRYJ": (25.4484, 81.8340),
    "ALD": (25.4484, 81.8340),
    "DDU": (25.2818, 83.1232),
    "MGS": (25.2818, 83.1232),
    "BSB": (25.3283, 82.9868),
    "GAYA": (24.8037, 85.0068),
    "PNME": (23.9575, 86.1132),
    "DHN": (23.7915, 86.4304),
    "ASN": (23.6841, 86.9745),
    "HWH": (22.5838, 88.3426),
    "SDAH": (22.5697, 88.3712),
    "MMCT": (18.9696, 72.8193),
    "BCT": (18.9696, 72.8193),
    "CSMT": (18.9401, 72.8353),
    "CSTM": (18.9401, 72.8353),
    "BDTS": (19.0622, 72.8407),
    "MAS": (13.0827, 80.2707),
    "SBC": (12.9784, 77.5684),
    "PUNE": (18.5284, 73.8739),
    "AGC": (27.1593, 77.9904),
    "GZB": (28.6675, 77.4414),
}

_LOADED_COORDS: Optional[Dict[str, Tuple[float, float]]] = None


def _get_coords() -> Dict[str, Tuple[float, float]]:
    global _LOADED_COORDS
    if _LOADED_COORDS is not None:
        return _LOADED_COORDS

    coords = dict(PRIMARY_STATION_COORDS)
    if STATIONS_JSON.exists():
        try:
            with open(STATIONS_JSON, encoding="utf-8") as f:
                features = json.load(f).get("features", [])
            for feat in features:
                code = feat.get("properties", {}).get("code")
                geom = feat.get("geometry")
                if code and geom and "coordinates" in geom:
                    lon, lat = geom["coordinates"]
                    if code not in coords:
                        coords[code] = (float(lat), float(lon))
        except Exception:
            pass

    _LOADED_COORDS = coords
    return _LOADED_COORDS


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the Great Circle distance in kilometers between two points."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def station_distance_km(from_code: str, to_code: str) -> int:
    """Return realistic track distance in km between two stations.

    Applies a ~1.18 winding factor to Haversine distance, with a minimum of 15 km.
    """
    coords = _get_coords()
    c1 = coords.get(from_code.upper())
    c2 = coords.get(to_code.upper())

    if c1 and c2:
        straight = haversine_distance(c1[0], c1[1], c2[0], c2[1])
        return max(15, int(round(straight * 1.18)))

    # Fallback if coordinates for either station are missing
    return 120


# Telescopic distance slab rates (limit_km, rate_per_km)
CLASS_SLABS: Dict[str, List[Tuple[float, float]]] = {
    "1A": [(300, 3.60), (700, 3.10), (float("inf"), 2.70)],
    "EC": [(300, 3.20), (700, 2.75), (float("inf"), 2.40)],
    "2A": [(300, 2.20), (700, 1.85), (float("inf"), 1.60)],
    "3A": [(300, 1.50), (700, 1.30), (float("inf"), 1.15)],
    "3E": [(300, 1.35), (700, 1.18), (float("inf"), 1.05)],
    "CC": [(300, 1.30), (700, 1.15), (float("inf"), 1.00)],
    "SL": [(300, 0.55), (700, 0.48), (float("inf"), 0.42)],
    "2S": [(300, 0.30), (700, 0.26), (float("inf"), 0.22)],
}

MIN_BASE_FARE: Dict[str, int] = {
    "1A": 550,
    "EC": 480,
    "2A": 360,
    "3A": 280,
    "3E": 250,
    "CC": 210,
    "SL": 120,
    "2S": 50,
}

RESERVATION_FEE: Dict[str, int] = {
    "1A": 60,
    "EC": 60,
    "2A": 50,
    "3A": 40,
    "3E": 40,
    "CC": 40,
    "SL": 20,
    "2S": 15,
}

SUPERFAST_SURCHARGE: Dict[str, int] = {
    "1A": 75,
    "EC": 75,
    "2A": 45,
    "3A": 45,
    "3E": 45,
    "CC": 45,
    "SL": 30,
    "2S": 15,
}

AC_CLASSES = {"1A", "EC", "2A", "3A", "3E", "CC"}


def calculate_base_fare(distance_km: float, travel_class: str) -> float:
    """Calculate base telescopic slab fare in INR."""
    cls = travel_class.upper()
    slabs = CLASS_SLABS.get(cls, CLASS_SLABS["3A"])
    min_fare = MIN_BASE_FARE.get(cls, MIN_BASE_FARE["3A"])

    fare = 0.0
    prev_limit = 0.0
    for limit, rate in slabs:
        if distance_km > prev_limit:
            slab_dist = min(distance_km, limit) - prev_limit
            fare += slab_dist * rate
            prev_limit = limit
        else:
            break

    return max(float(min_fare), fare)


def calculate_stretch_fare(
    from_station: str,
    to_station: str,
    travel_class: str,
    is_superfast: bool = True,
    distance_km: Optional[int] = None,
) -> dict:
    """Calculate realistic fare breakdown for one stretch (from_station -> to_station)."""
    cls = travel_class.upper()
    if distance_km is None:
        distance_km = station_distance_km(from_station, to_station)

    base = calculate_base_fare(distance_km, cls)
    res_fee = RESERVATION_FEE.get(cls, 20)
    sf_fee = SUPERFAST_SURCHARGE.get(cls, 30) if is_superfast else 0

    subtotal = base + res_fee + sf_fee
    is_ac = cls in AC_CLASSES
    gst = round(subtotal * 0.05, 2) if is_ac else 0.0

    raw_total = subtotal + gst
    # Indian Railways rounds fare to nearest multiple of ₹5
    total = int(round(raw_total / 5.0) * 5)
    if total <= 0:
        total = 5

    return {
        "from_station": from_station,
        "to_station": to_station,
        "travel_class": cls,
        "distance_km": distance_km,
        "base_fare": round(base, 2),
        "reservation_charge": res_fee,
        "superfast_charge": sf_fee,
        "gst": round(gst, 2),
        "total_fare": total,
    }


def calculate_journey_fare(
    legs: tuple,
    preferred_classes: Optional[Tuple[str, ...]] = None,
    default_class: str = "3A",
) -> Tuple[int, Tuple[dict, ...]]:
    """Compute total INR fare and per-stretch breakdown for an entire candidate journey.

    Handles both:
    1. Multi-stretch same-train seat-hopping legs (where leg.split is non-empty).
    2. Direct or multi-train journeys (where leg.seats has available classes).
    """
    breakdown: List[dict] = []
    total_fare = 0

    for leg in legs:
        if leg.split:
            # Segmented multi-stretch plan
            for piece in leg.split:
                if getattr(piece, "fare_breakdown", None):
                    stretch_fare = dict(piece.fare_breakdown)
                    stretch_fare.setdefault("travel_class", piece.travel_class)
                else:
                    stretch_fare = calculate_stretch_fare(
                        piece.from_station,
                        piece.to_station,
                        piece.travel_class,
                    )
                breakdown.append(stretch_fare)
                total_fare += stretch_fare.get("total_fare", getattr(piece, "fare_inr", 0))
        else:
            # Whole leg single class: pick representative class
            chosen_cls = default_class
            if preferred_classes:
                # Pick the first preferred class that has seats
                for pc in preferred_classes:
                    if pc in leg.seats:
                        chosen_cls = pc
                        break
                else:
                    chosen_cls = preferred_classes[0]
            elif leg.seats:
                # Prefer 3A > SL > 2A > others
                priority = ["3A", "SL", "2A", "CC", "3E", "1A", "EC", "2S"]
                for p in priority:
                    if p in leg.seats:
                        chosen_cls = p
                        break
                else:
                    chosen_cls = next(iter(leg.seats.keys()))

            stretch_fare = calculate_stretch_fare(
                leg.from_station,
                leg.to_station,
                chosen_cls,
            )
            breakdown.append(stretch_fare)
            total_fare += stretch_fare["total_fare"]

    return total_fare, tuple(breakdown)
