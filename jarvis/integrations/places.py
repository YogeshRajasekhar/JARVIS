"""
Places and travel time. Free: OpenStreetMap Nominatim (geocoding) + OSRM (routing).
Mock provider uses a small gazetteer and a distance-based estimate (also used as fallback).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

SPEED_KMH = {"driving": 22.0, "transit": 18.0, "cycling": 13.0, "walking": 4.5}  # city averages incl. traffic
DETOUR = 1.35  # road distance / straight-line distance


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    address: str = ""


def haversine_km(a: Place, b: Place) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimate_minutes(a: Place, b: Place, mode: str = "driving") -> tuple[float, float]:
    km = haversine_km(a, b) * DETOUR
    return km / SPEED_KMH.get(mode, 22.0) * 60 + 5, km  # +5 min for parking/walking to the door


class PlacesProvider(ABC):
    @abstractmethod
    def geocode(self, name: str, near: Optional[Place] = None) -> Optional[Place]: ...

    def route(self, a: Place, b: Place, mode: str = "driving") -> dict:
        minutes, km = estimate_minutes(a, b, mode)
        return {"minutes": round(minutes), "km": round(km, 1), "mode": mode, "source": "estimate"}

    def nearby(self, center: Place, kind: str = "cafe") -> list[Place]:
        return []

    def midpoint(self, places: list[Place]) -> Place:
        lat = sum(p.lat for p in places) / len(places)
        lon = sum(p.lon for p in places) / len(places)
        return Place("midpoint", lat, lon)


GAZETTEER = {
    "iit madras": Place("IIT Madras", 12.9916, 80.2336, "Sardar Patel Rd, Chennai"),
    "inspire lab": Place("INSPIRE Lab, IIT Madras", 12.9905, 80.2310, "IIT Madras, Chennai"),
    "guindy": Place("Guindy", 13.0067, 80.2206, "Chennai"),
    "adyar": Place("Adyar", 13.0012, 80.2565, "Chennai"),
    "velachery": Place("Velachery", 12.9791, 80.2210, "Chennai"),
    "t nagar": Place("T. Nagar", 13.0418, 80.2341, "Chennai"),
    "chennai airport": Place("Chennai Airport", 12.9941, 80.1709, "Meenambakkam, Chennai"),
    "chennai central": Place("Chennai Central", 13.0827, 80.2757, "Park Town, Chennai"),
    "besant nagar": Place("Besant Nagar Beach", 13.0002, 80.2668, "Chennai"),
    "phoenix mall": Place("Phoenix Marketcity", 12.9915, 80.2170, "Velachery, Chennai"),
    "tu darmstadt": Place("TU Darmstadt", 49.8775, 8.6553, "Karolinenplatz 5, Darmstadt"),
    "darmstadt hbf": Place("Darmstadt Hauptbahnhof", 49.8725, 8.6295, "Darmstadt"),
    "home": Place("Home", 12.9850, 80.2400, "Chennai"),
}
CAFES = [
    Place("Amethyst Cafe", 13.0435, 80.2512, "Royapettah, Chennai"),
    Place("Writer's Cafe", 13.0010, 80.2550, "Adyar, Chennai"),
    Place("Ciclo Cafe", 13.0063, 80.2575, "Adyar, Chennai"),
    Place("Chamiers Cafe", 13.0200, 80.2530, "RA Puram, Chennai"),
    Place("Brew Room", 12.9900, 80.2200, "Velachery, Chennai"),
    Place("Starbucks Phoenix", 12.9915, 80.2172, "Velachery, Chennai"),
]


class MockPlaces(PlacesProvider):
    def geocode(self, name, near=None):
        q = name.strip().lower().replace(".", "")
        if q in GAZETTEER:
            return GAZETTEER[q]
        for k, p in GAZETTEER.items():
            if q in k or k in q:
                return p
        for c in CAFES:
            if q in c.name.lower():
                return c
        return None

    def nearby(self, center, kind="cafe"):
        return sorted(CAFES, key=lambda c: haversine_km(center, c))[:3]


class OSMPlaces(PlacesProvider):
    """
    Nominatim usage policy: max 1 request/second, identify the app. Fine for one person.
    OSRM public demo server: driving profile; other modes fall back to the estimate.
    """

    def __init__(self, contact_email: str = ""):
        ua = "JARVIS-personal-assistant/2.0" + (f" ({contact_email})" if contact_email else "")
        self.c = httpx.Client(headers={"User-Agent": ua}, timeout=15)
        self.mock = MockPlaces()

    def geocode(self, name, near=None):
        hit = self.mock.geocode(name)
        if hit and name.lower() in GAZETTEER:
            return hit
        params = {"q": name, "format": "jsonv2", "limit": 1}
        if near:
            params.update({"viewbox": f"{near.lon-0.3},{near.lat+0.3},{near.lon+0.3},{near.lat-0.3}", "bounded": 0})
        try:
            j = self.c.get("https://nominatim.openstreetmap.org/search", params=params).json()
        except httpx.HTTPError:
            return hit
        if not j:
            return hit
        return Place(j[0].get("name") or name, float(j[0]["lat"]), float(j[0]["lon"]), j[0].get("display_name", ""))

    def route(self, a, b, mode="driving"):
        if mode != "driving":
            return super().route(a, b, mode)
        try:
            j = self.c.get(f"https://router.project-osrm.org/route/v1/driving/{a.lon},{a.lat};{b.lon},{b.lat}",
                           params={"overview": "false"}).json()
            r = j["routes"][0]
            # OSRM has no live traffic: scale by 1.4 for Indian city traffic.
            return {"minutes": round(r["duration"] / 60 * 1.4), "km": round(r["distance"] / 1000, 1),
                    "mode": mode, "source": "osrm"}
        except Exception:
            return super().route(a, b, mode)

    def nearby(self, center, kind="cafe"):
        try:
            j = self.c.get("https://nominatim.openstreetmap.org/search", params={
                "q": kind, "format": "jsonv2", "limit": 3, "bounded": 1,
                "viewbox": f"{center.lon-0.02},{center.lat+0.02},{center.lon+0.02},{center.lat-0.02}"}).json()
            return [Place(x.get("name") or kind, float(x["lat"]), float(x["lon"]), x.get("display_name", "")) for x in j]
        except Exception:
            return self.mock.nearby(center, kind)
