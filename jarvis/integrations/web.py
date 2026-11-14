"""
The open web, read-only, from free keyless services:

  weather    Open-Meteo (geocoding + forecast)
  knowledge  Wikipedia (search + page summary)
  news       Google News RSS
  search     DuckDuckGo's HTML results (best effort; Wikipedia is the fallback)
  currency   open.er-api.com daily rates

Everything that comes back is untrusted text written by strangers: callers strip it to plain text,
cap its length, drop anything that looks like an instruction to the assistant, and only ever show it
or summarise it. Nothing fetched here can trigger an action.

Only the search words leave the server (never names from memory, mail, or the calendar); the
"web" permission tells the user that before the first lookup.
"""

from __future__ import annotations

import html
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree

import httpx

from jarvis.core.redaction import injection_score

UA = {"User-Agent": "JARVIS-personal-assistant/2 (private, single user)"}
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def clean(text: str, limit: int = 400) -> str:
    """HTML/markup -> one line of plain text, capped. Applied to everything fetched from the web."""
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = html.unescape(t)
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit].rsplit(" ", 1)[0] + "..." if len(t) > limit else t


def safe(text: str) -> bool:
    return not injection_score(text or "")


class WebError(Exception):
    pass


class WebProvider(ABC):
    @abstractmethod
    def weather(self, place: Optional[str], lat: Optional[float] = None, lon: Optional[float] = None) -> dict: ...

    @abstractmethod
    def lookup(self, query: str) -> Optional[dict]: ...

    @abstractmethod
    def news(self, topic: Optional[str], limit: int = 5) -> list[dict]: ...

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[dict]: ...

    @abstractmethod
    def rate(self, src: str, dst: str) -> Optional[float]: ...


class LiveWeb(WebProvider):
    def __init__(self, country: str = "IN", lang: str = "en", transport=None):
        self.country, self.lang = country, lang
        self._http = httpx.Client(timeout=8.0, headers=UA, follow_redirects=True, transport=transport)
        self._cache: dict = {}

    def _get(self, url: str, params: dict, ttl: int = 300):
        key = (url, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            r = self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise WebError(f"couldn't reach {httpx.URL(url).host}") from e
        if r.status_code >= 400:
            raise WebError(f"{httpx.URL(url).host} answered HTTP {r.status_code}")
        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), r)
        return r

    # ------------------------------------------------------------------ weather
    def weather(self, place=None, lat=None, lon=None) -> dict:
        name = place
        if lat is None or lon is None:
            if not place:
                raise WebError("no place given")
            g = self._get("https://geocoding-api.open-meteo.com/v1/search", {"name": place, "count": 1, "language": "en"}, 86400).json()
            hits = g.get("results") or []
            if not hits:
                raise WebError(f"I couldn't find a place called {place}")
            lat, lon = hits[0]["latitude"], hits[0]["longitude"]
            name = ", ".join(x for x in [hits[0].get("name"), hits[0].get("country")] if x)
        j = self._get("https://api.open-meteo.com/v1/forecast", {
            "latitude": round(lat, 3), "longitude": round(lon, 3), "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"}, 900).json()
        cur, d = j.get("current", {}), j.get("daily", {})
        days = []
        for i, date in enumerate(d.get("time", [])):
            days.append({"date": date, "summary": WMO.get(d["weather_code"][i], "mixed"), "max": round(d["temperature_2m_max"][i]),
                         "min": round(d["temperature_2m_min"][i]), "rain_chance": d.get("precipitation_probability_max", [None] * 9)[i],
                         "rain_mm": d.get("precipitation_sum", [0] * 9)[i]})
        return {"place": name or "your location",
                "now": {"temp": round(cur.get("temperature_2m", 0)), "feels": round(cur.get("apparent_temperature", 0)),
                        "summary": WMO.get(cur.get("weather_code"), "mixed"), "humidity": cur.get("relative_humidity_2m"),
                        "wind": round(cur.get("wind_speed_10m", 0))},
                "days": days}

    # ------------------------------------------------------------------ knowledge
    def lookup(self, query: str) -> Optional[dict]:
        s = self._get(f"https://{self.lang}.wikipedia.org/w/api.php",
                      {"action": "query", "list": "search", "srsearch": query, "srlimit": 1, "format": "json"}, 3600).json()
        hits = s.get("query", {}).get("search", [])
        if not hits:
            return None
        title = hits[0]["title"]
        p = self._get(f"https://{self.lang}.wikipedia.org/w/api.php",
                      {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
                       "titles": title, "format": "json"}, 3600).json()
        pages = p.get("query", {}).get("pages", {})
        extract = next(iter(pages.values()), {}).get("extract", "") if pages else ""
        extract = clean(extract, 1200)
        if not extract or not safe(extract):
            return None
        return {"title": clean(title, 80), "extract": extract, "source": "Wikipedia",
                "url": f"https://{self.lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"}

    # ------------------------------------------------------------------ news
    def news(self, topic=None, limit=5) -> list[dict]:
        loc = {"hl": f"{self.lang}-{self.country}", "gl": self.country, "ceid": f"{self.country}:{self.lang}"}
        if topic:
            r = self._get("https://news.google.com/rss/search", {"q": topic, **loc}, 600)
        else:
            r = self._get("https://news.google.com/rss", loc, 600)
        try:
            root = ElementTree.fromstring(r.content)
        except ElementTree.ParseError as e:
            raise WebError("the news feed was unreadable") from e
        out = []
        for it in root.iter("item"):
            title = clean(it.findtext("title") or "", 160)
            src = clean(it.findtext("source") or "", 40)
            if src and title.endswith(f" - {src}"):
                title = title[: -len(src) - 3]
            if not title or not safe(title):
                continue
            link = (it.findtext("link") or "").strip()
            out.append({"title": title, "source": src, "url": link if link.startswith("https://") else "",
                        "when": clean(it.findtext("pubDate") or "", 40)})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit=5) -> list[dict]:
        try:
            r = self._http.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": f"{self.country.lower()}-{self.lang}"})
            if r.status_code != 200:
                raise WebError("search unavailable")
        except httpx.HTTPError as e:
            raise WebError("search unavailable") from e
        out = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
            url, title, snippet = html.unescape(m.group(1)), clean(m.group(2), 120), clean(m.group(3), 260)
            real = re.search(r"uddg=([^&]+)", url)
            if real:
                url = httpx.URL("https://x/?u=" + real.group(1)).params["u"]
            if not url.startswith("https://") or not title or not safe(title + " " + snippet):
                continue
            out.append({"title": title, "snippet": snippet, "url": url, "source": httpx.URL(url).host.removeprefix("www.")})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ currency
    def rate(self, src: str, dst: str) -> Optional[float]:
        j = self._get(f"https://open.er-api.com/v6/latest/{src.upper()}", {}, 6 * 3600).json()
        v = (j.get("rates") or {}).get(dst.upper())
        return float(v) if v else None


class MockWeb(WebProvider):
    """Deterministic stand-in for tests and the demo phone (this sandbox has no open internet)."""

    def __init__(self):
        self.queries: list[tuple[str, str]] = []

    def weather(self, place=None, lat=None, lon=None) -> dict:
        self.queries.append(("weather", place or "here"))
        if place and place.lower() in {"atlantis", "nowhere"}:
            raise WebError(f"I couldn't find a place called {place}")
        base = datetime(2026, 1, 1)
        days = [{"date": f"day{i}", "summary": ["partly cloudy", "rain", "clear", "showers", "clear", "overcast", "clear"][i],
                 "max": 31 + i % 3, "min": 24 + i % 2, "rain_chance": [20, 80, 5, 60, 10, 30, 0][i], "rain_mm": [0, 12, 0, 4, 0, 1, 0][i]}
                for i in range(7)]
        del base
        return {"place": (place if any(c.isupper() for c in place) else place.title()) if place else "IIT Madras",
                "now": {"temp": 30, "feels": 34, "summary": "partly cloudy", "humidity": 70, "wind": 12}, "days": days}

    def lookup(self, query: str) -> Optional[dict]:
        self.queries.append(("lookup", query))
        q = query.lower()
        if "langgraph" in q:
            return {"title": "LangGraph", "extract": "LangGraph is an open-source library for building stateful, multi-agent "
                    "applications with large language models. It models workflows as graphs.", "source": "Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/LangGraph"}
        if "japan" in q:
            return {"title": "Prime Minister of Japan", "extract": "The prime minister of Japan is the head of government of Japan. "
                    "The prime minister chairs the Cabinet and is appointed by the Emperor after being nominated by the National Diet.",
                    "source": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Prime_Minister_of_Japan"}
        if "xyzzy" in q:
            return None
        return {"title": query.title(), "extract": f"{query.capitalize()} is a topic with a demo encyclopedia entry. It has a short summary.",
                "source": "Wikipedia", "url": "https://en.wikipedia.org/wiki/" + query.replace(" ", "_")}

    def news(self, topic=None, limit=5) -> list[dict]:
        self.queries.append(("news", topic or "top"))
        t = topic or "India"
        return [{"title": f"{t}: demo headline {i + 1}", "source": ["The Hindu", "Reuters", "NDTV"][i % 3],
                 "url": f"https://example.com/news/{i + 1}", "when": ""} for i in range(min(limit, 3))]

    def search(self, query: str, limit=5) -> list[dict]:
        self.queries.append(("search", query))
        return [{"title": f"{query} - result {i + 1}", "snippet": f"A demo page about {query}.", "url": f"https://example.com/{i + 1}",
                 "source": "example.com"} for i in range(min(limit, 3))]

    def rate(self, src: str, dst: str) -> Optional[float]:
        table = {"USD": 1.0, "INR": 84.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AED": 3.67}
        s, d = table.get(src.upper()), table.get(dst.upper())
        return d / s if s and d else None
