"""
Web agent: weather, news, facts and currency from the open internet, and Apps agent: things that open
on the phone (YouTube, Spotify, WhatsApp, Maps navigation, websites) plus volume and media keys.

Web results are untrusted. They are cleaned, capped, screened for injected instructions, and then only
read out or summarised by a prompt that treats them as quoted source text. Nothing from the web can
start an action.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import timedelta
from typing import Literal, Optional

from pydantic import Field

from jarvis.agents.base import AgentError, AgentReply, BaseAgent, TurnContext
from jarvis.core.permissions import is_blocked_app
from jarvis.core.redaction import redact
from jarvis.core.timeparse import resolve
from jarvis.integrations.web import WebError
from jarvis.llm.schemas import _Lenient
from jarvis.prompts import APPS, WEB, WEB_ANSWER

# ============================================================================ web
CURRENCIES = {
    "usd": "USD", "dollar": "USD", "dollars": "USD", "$": "USD", "us dollar": "USD", "us dollars": "USD",
    "inr": "INR", "rupee": "INR", "rupees": "INR", "rs": "INR", "₹": "INR",
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR", "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "£": "GBP",
    "jpy": "JPY", "yen": "JPY", "aed": "AED", "dirham": "AED", "dirhams": "AED", "chf": "CHF", "franc": "CHF", "francs": "CHF",
    "sgd": "SGD", "aud": "AUD", "cad": "CAD", "cny": "CNY", "yuan": "CNY",
}
CUR = r"(?:us dollars?|usd|dollars?|\$|inr|rupees?|rs|₹|eur|euros?|€|gbp|pounds?|£|jpy|yen|aed|dirhams?|chf|francs?|sgd|aud|cad|cny|yuan)"
WEATHER_WORDS = r"weather|forecast|temperature|rain(?:ing|y)?|umbrella|sunny|humid(?:ity)?|snow(?:ing)?|storm|how (?:hot|cold|warm)|raincoat|jacket"
DAY_WORDS = r"today|tonight|now|tomorrow|day after tomorrow|this weekend|weekend|this week|next week|monday|tuesday|wednesday|thursday|friday|saturday|sunday"


class WebOut(_Lenient):
    action: Literal["weather", "news", "lookup", "search", "convert", "unknown"]
    query: Optional[str] = None       # topic / question / search words
    place: Optional[str] = None       # weather location
    day: Optional[str] = None         # weather day phrase
    amount: Optional[float] = None
    src: Optional[str] = None
    dst: Optional[str] = None


def parse_web(text: str) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.strip()).rstrip("?.! ")
    low = t.lower()
    # ---- currency
    m = re.match(rf"^(?:convert |what(?:'s| is) |how much is )?(?P<n>[\d,]*\.?\d+)\s*(?P<a>{CUR})\s+(?:to|in|into)\s+(?P<b>{CUR})$", low) \
        or re.match(rf"^(?:what(?:'s| is) )?(?:the )?(?P<a>{CUR})(?: to | vs | against |/| in )(?P<b>{CUR})(?: (?:exchange )?rate)?(?: today| now)?$", low) \
        or re.match(rf"^(?:what(?:'s| is) )?(?:the )?(?:current |today'?s )?(?P<a>{CUR}) (?:exchange )?rate(?: today| now)?$", low)
    if m:
        d = m.groupdict()
        a = CURRENCIES.get(d["a"])
        b = CURRENCIES.get(d.get("b") or "") or ("INR" if a != "INR" else "USD")
        return {"action": "convert", "amount": float((d.get("n") or "1").replace(",", "")), "src": a, "dst": b}
    # ---- weather
    if re.search(rf"\b({WEATHER_WORDS})\b", low) and not re.search(r"\b(email|mail|remind|schedule|note|remember)\b", low):
        place = None
        pm = re.search(r"\b(?:in|at|for|near) (?!the (?:morning|evening|afternoon|night)\b)([a-z][a-z .'-]{1,40}?)(?=\s+(?:" + DAY_WORDS + r"|on|at|this|next|right)\b|$)", low)
        if pm and not re.fullmatch(rf"(?:{DAY_WORDS}|the next \w+|a few days)", pm.group(1).strip()):
            place = pm.group(1).strip()
        dm = re.search(rf"\b({DAY_WORDS})\b", low)
        return {"action": "weather", "place": place, "day": dm.group(1) if dm else None}
    # ---- news
    if re.search(r"\b(news|headlines)\b", low) and not re.search(r"\b(email|mail|remind|schedule|remember|tell \w+ the news|good news|bad news)\b", low):
        q = ""
        m = re.search(r"\b(?:news|headlines)(?: today| now| this morning)? (?:about|on|for|from|in|regarding|of) (?P<q>.+?)(?: today| now)?$", low) \
            or re.match(r"^(?:what(?:'s| is| are)|show|give|tell|read|any|get)?(?: me)?(?: the)?(?: latest| top| recent| today'?s| current)* ?(?P<q>.+?) (?:news|headlines)(?: today| now)?$", low)
        if m:
            q = re.sub(r"^(?:the |latest |top |recent |today'?s |any |some |me |what'?s |whats )+", "", t[m.start("q"):m.end("q")].strip(), flags=re.I).strip()
            if q.lower() in {"the", "latest", "top", "today's", "todays", "any", "recent", "what's the", "more"}:
                q = ""
        return {"action": "news", "query": q or None}
    m = re.match(r"^what(?:'s| is) (?:happening|going on|new) (?:in|with|at) (?P<q>.+)$", low)
    if m and not re.search(r"\b(my|our)\b", low):
        return {"action": "news", "query": t[m.start("q"):m.end("q")]}
    # ---- explicit search
    m = re.match(r"^(?:search|look|google|find|browse)(?: up)?(?: the| on the)? ?(?:web|internet|online|net)?(?: for| about)? (?P<q>.+)$", t, re.I)
    if m and re.match(r"^(?:search(?: the| on the)? (?:web|internet|online|net)|search for|look up|search up|web search|look online)", low):
        return {"action": "search", "query": m.group("q")}
    # ---- knowledge
    m = re.match(r"^(?:who (?:is|was|are|were)|what (?:is|are|was|were)|what's|who's|tell me about|explain|define|"
                 r"what does .+ mean|where is|when (?:was|is|did)|how (?:tall|old|big|far|long|many|much) (?:is|are|was|did|does)|"
                 r"capital of|population of|meaning of) ?(?P<q>.*)$", low)
    if m and not re.search(r"\b(my|me|i|our|we|you|your|jarvis)\b", low):
        return {"action": "lookup", "query": t}
    return None


def needs_fresh_facts(text: str) -> bool:
    """Questions whose answer changes over time: the model's memory is stale, so look them up."""
    return bool(re.search(r"\b(current|currently|latest|newest|recent|today|right now|this year|who is the (?:president|prime minister|pm|ceo|"
                          r"chief minister|cm|captain|governor|king|queen|chancellor|mayor|head)|price of|stock price|score|won|winner|"
                          r"release date|released|population of|net worth|exchange rate|who won)\b", text, re.I))


class WebAgent(BaseAgent):
    name = "web"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd = parse_web(text)
        if cmd is None:
            try:
                cmd = self.extract(WebOut, WEB, text, ctx, task="web").model_dump()
            except AgentError:
                cmd = {"action": "lookup", "query": text}
        a = cmd.get("action")
        try:
            if a == "weather":
                return self._weather(cmd, ctx)
            if a == "convert":
                return self._convert(cmd, ctx)
            if a == "news":
                return self._news(cmd, ctx)
            if a in ("lookup", "search"):
                return self._answer(cmd.get("query") or text, text, ctx, prefer_search=a == "search")
        except WebError as e:
            return AgentReply(self.name, f"I couldn't get that from the web just now ({e}). Try again in a moment.")
        return AgentReply(self.name, "I can check the weather, the news, currency rates and look things up. What do you want to know?")

    # ------------------------------------------------------------------ weather
    def _weather(self, cmd, ctx) -> AgentReply:
        place = cmd.get("place")
        lat = lon = None
        outs = []
        if not place:
            home = self.svc.db.kv_get("home_city")
            loc = self.propose("device.location", {}, ctx)
            if loc.status == "done" and loc.data.get("lat") is not None:
                lat, lon, place = loc.data["lat"], loc.data["lon"], loc.data.get("name")
            elif home:
                place = home
            elif loc.status == "needs_permission":
                return AgentReply(self.name, "Which city? Or allow location and I'll use where you are.", [loc], clarify="place")
            else:
                return AgentReply(self.name, "Which city should I check?", clarify="place")
        out = self.propose("web.weather", {"place": place, "lat": lat, "lon": lon}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        outs.append(out)
        w = out.data
        name = w["place"]
        day = (cmd.get("day") or "today").lower()
        days = w.get("days") or []
        idx = 0
        if day in ("tomorrow",):
            idx = 1
        elif day == "day after tomorrow":
            idx = 2
        elif day not in ("today", "tonight", "now", "this week", "next week", "weekend", "this weekend"):
            r = resolve(day, ctx.now)
            if r:
                idx = max(0, min(6, (r.start.date() - ctx.now.date()).days))
        card = {"type": "weather", "place": name, "now": w["now"], "days": days[:5]}
        if day in ("this week", "next week") and days:
            wet = [d for d in days if (d.get("rain_chance") or 0) >= 50]
            hi, lo = max(d["max"] for d in days), min(d["min"] for d in days)
            text = f"This week in {name}: highs up to {hi}°, lows around {lo}°. " + \
                   (f"Rain is likely on {len(wet)} of the next {len(days)} days." if wet else "No real rain expected.")
            return AgentReply(self.name, text, outs, cards=[card])
        if day in ("weekend", "this weekend") and days:
            wk = [(i, d) for i, d in enumerate(days) if (ctx.now + timedelta(days=i)).weekday() >= 5][:2]
            if wk:
                bits = [f"{(ctx.now + timedelta(days=i)).strftime('%A')} {d['summary']}, {d['max']}°" +
                        (f", {d['rain_chance']}% chance of rain" if (d.get('rain_chance') or 0) >= 30 else "") for i, d in wk]
                return AgentReply(self.name, f"The weekend in {name}: " + "; ".join(bits) + ".", outs, cards=[card])
        if idx == 0 or idx >= len(days):
            n = w["now"]
            d0 = days[0] if days else None
            text = f"It's {n['temp']}° and {n['summary']} in {name}" + (f", feels like {n['feels']}°" if abs(n["feels"] - n["temp"]) >= 2 else "") + "."
            if d0:
                text += f" Today's high is {d0['max']}°, low {d0['min']}°" + \
                        (f", with a {d0['rain_chance']}% chance of rain. Take an umbrella." if (d0.get("rain_chance") or 0) >= 50 else
                         f", {d0['rain_chance']}% chance of rain." if d0.get("rain_chance") is not None else ".")
        else:
            d = days[idx]
            label = "Tomorrow" if idx == 1 else (ctx.now + timedelta(days=idx)).strftime("%A")
            rain = d.get("rain_chance")
            text = f"{label} in {name}: {d['summary']}, high {d['max']}°, low {d['min']}°" + \
                   (f". Rain is likely ({rain}%), so carry an umbrella." if (rain or 0) >= 50 else f", {rain}% chance of rain." if rain is not None else ".")
        return AgentReply(self.name, text, outs, cards=[card])

    # ------------------------------------------------------------------ currency
    def _convert(self, cmd, ctx) -> AgentReply:
        src, dst = (cmd.get("src") or "").upper(), (cmd.get("dst") or "").upper()
        if not src or not dst:
            return AgentReply(self.name, "Which two currencies?", clarify="content")
        out = self.propose("web.rate", {"src": src, "dst": dst}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        rate = out.data.get("rate")
        if not rate:
            return AgentReply(self.name, f"I couldn't find a rate for {src} to {dst}.", [out])
        amt = float(cmd.get("amount") or 1)
        val = amt * rate
        return AgentReply(self.name, f"{amt:,.2f}".rstrip("0").rstrip(".") + f" {src} is about {val:,.2f} {dst} at today's rate.", [out])

    # ------------------------------------------------------------------ news
    def _news(self, cmd, ctx) -> AgentReply:
        topic = cmd.get("query")
        out = self.propose("web.news", {"topic": redact(topic) if topic else None}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        items = out.data.get("items", [])
        if not items:
            return AgentReply(self.name, f"No recent news found{' about ' + topic if topic else ''}.", [out])
        lead = f"Top stories{' on ' + topic if topic else ''}: "
        text = lead + " ".join(f"{i + 1}) {it['title']}" + (f" ({it['source']})." if it.get("source") else ".") for i, it in enumerate(items[:3]))
        return AgentReply(self.name, text, [out], cards=[{"type": "links", "title": f"News{': ' + topic if topic else ''}",
                                                           "items": [{"title": it["title"], "sub": it.get("source", ""), "url": it.get("url", "")} for it in items[:5]]}])

    # ------------------------------------------------------------------ facts
    def _answer(self, query: str, question: str, ctx, prefer_search=False) -> AgentReply:
        q = redact(_topic(query))
        outs = []
        src = None
        if not prefer_search:
            out = self.propose("web.lookup", {"query": q}, ctx)
            if (b := self.blocked_reply(out)):
                return b
            outs.append(out)
            src = out.data.get("page")
        results = []
        if src is None:
            try:
                out = self.propose("web.search", {"query": q}, ctx)
                if (b := self.blocked_reply(out)):
                    return b
                outs.append(out)
                results = out.data.get("results", [])
            except WebError:
                results = []
            if not results and prefer_search:
                out = self.propose("web.lookup", {"query": q}, ctx)
                if out.status == "done":
                    outs.append(out)
                    src = out.data.get("page")
        if src is None and not results:
            return AgentReply(self.name, f"I couldn't find anything reliable about {q}.", outs)
        if src is not None:
            source_text = f"{src['title']} ({src['source']}): {src['extract']}"
            links = [{"title": src["title"], "sub": src["source"], "url": src["url"]}]
        else:
            source_text = "\n".join(f"- {r['title']} ({r['source']}): {r['snippet']}" for r in results[:4])
            links = [{"title": r["title"], "sub": r["source"], "url": r["url"]} for r in results[:4]]
        try:
            ans = self.svc.smart_llm.complete(f"Question: {redact(question)}", system=WEB_ANSWER.format(source=source_text),
                                              temperature=0.0, max_tokens=180, task="web_answer")
        except Exception:
            ans = ""
        ans = re.sub(r"[*_#`]", "", ans or "").strip()
        if not ans or ans.startswith(("I'm in offline mode", "I can't reach", "I didn't understand that")):
            if src is not None:
                ans = _first_sentences(src["extract"], 2) + f" (from {src['source']})"
            else:
                ans = f"Here's what I found: {results[0]['title']}. {results[0]['snippet']}"
        return AgentReply(self.name, ans, outs, cards=[{"type": "links", "title": "Sources", "items": links}])


def _topic(q: str) -> str:
    """'who is the prime minister of japan?' -> 'prime minister of japan' (better encyclopedia search terms)."""
    t = re.sub(r"\s+", " ", q.strip()).rstrip("?.! ")
    t = re.sub(r"^(?:please )?(?:search|look up|google|find)(?: the| on the)? ?(?:web|internet|online|net)?(?: for| about)? ", "", t, flags=re.I)
    t = re.sub(r"^(?:who|what|where|when|which)(?:'s| is| are| was| were| does| did)? (?:the |a |an )?", "", t, flags=re.I)
    t = re.sub(r"^(?:tell me about|explain|define|meaning of) (?:the |a |an )?", "", t, flags=re.I)
    t = re.sub(r"\s+mean$", "", t)
    return t.strip()[:120] or q[:120]


def _first_sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return " ".join(parts[:n]).strip()


# ============================================================================ apps
class AppsOut(_Lenient):
    action: Literal["play", "whatsapp", "navigate", "open_url", "media", "volume", "unknown"]
    query: Optional[str] = None       # what to play / destination / site
    app: Optional[str] = None         # youtube | spotify | youtube music
    contact: Optional[str] = None
    message: Optional[str] = None
    control: Optional[str] = None     # media: play|pause|next|previous|stop ; volume: up|down|mute|unmute|max|set
    level: Optional[int] = Field(None, ge=0, le=100)


SITE_RX = re.compile(r"^(?:https?://)?(?:www\.)?(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|in|io|ai|dev|edu|gov|co|de|uk|app|me|tv|info|ac\.in|co\.in|co\.uk))(?P<path>/[\w\-./?=&%+#~]*)?$", re.I)
PLAY_APPS = {"youtube": "youtube", "yt": "youtube", "you tube": "youtube", "youtube music": "ytmusic", "yt music": "ytmusic", "spotify": "spotify"}


def parse_apps(text: str) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.strip()).rstrip("?.! ")
    low = t.lower()
    # ---- volume
    m = re.match(r"^(?:set|change|turn|put)(?: the)? volume (?:to|at) (?P<n>\d{1,3})\s*(?:%|percent)?$", low) \
        or re.match(r"^volume (?:to |at )?(?P<n>\d{1,3})\s*(?:%|percent)?$", low)
    if m:
        return {"action": "volume", "control": "set", "level": min(100, int(m.group("n")))}
    if re.match(r"^(?:turn |crank )?(?:the )?volume up|^(?:increase|raise|turn up|louder)(?: the)?(?: volume)?$|^(?:make it |a bit )?louder$|^turn it up$", low):
        return {"action": "volume", "control": "up"}
    if re.match(r"^(?:turn )?(?:the )?volume down|^(?:decrease|lower|reduce|turn down|quieter)(?: the)?(?: volume)?$|^(?:make it |a bit )?(?:quieter|softer)$|^turn it down$", low):
        return {"action": "volume", "control": "down"}
    if re.match(r"^(?:max(?:imum)?|full) volume$|^volume (?:to )?(?:max|full)$", low):
        return {"action": "volume", "control": "max"}
    if re.match(r"^(?:mute|silence)(?: the)?(?: phone| volume| sound| music| it)?$", low):
        return {"action": "volume", "control": "mute"}
    if re.match(r"^unmute(?: the)?(?: phone| volume| sound| music| it)?$", low):
        return {"action": "volume", "control": "unmute"}
    # ---- media keys
    if re.match(r"^pause(?: the| this)?(?: music| song| video| playback| it)?$", low):
        return {"action": "media", "control": "pause"}
    if re.match(r"^(?:resume|unpause|play)(?: the| my)?(?: music| song| video| playback| it)?$|^continue (?:the |my )?(?:music|song|video|playback)$", low):
        return {"action": "media", "control": "play"}
    if re.match(r"^(?:next|skip)(?: the| this)? (?:song|track|one|video)$|^skip$|^(?:play |skip to )(?:the )?next(?: song| track)?$", low):
        return {"action": "media", "control": "next"}
    if re.match(r"^(?:previous|prev)(?: song| track)?$|^(?:go back to |play )(?:the )?(?:previous|last) (?:song|track)$", low):
        return {"action": "media", "control": "previous"}
    if re.match(r"^stop(?: the| this)? (?:music|song|video|playback)$", low):
        return {"action": "media", "control": "stop"}
    # ---- whatsapp
    m = re.match(r"^(?:send (?:a )?whatsapp(?: message)? to|whatsapp(?: message)?(?: to)?|wa|message on whatsapp to) (?P<who>[\w .']+?)"
                 r"(?:\s*(?:that|saying|say|to say|:|,| - )\s*(?P<msg>.+))?$", t, re.I) \
        or re.match(r"^(?:message|text|ping|tell) (?P<who>[\w .']+?) (?:on|via|over|in|through) whatsapp(?:\s*(?:that|saying|say|:|,)?\s*(?P<msg>.+))?$", t, re.I) \
        or re.match(r"^(?:tell|message|text) (?P<who>[\w .']+?) (?:that |saying )?(?P<msg>.+?) (?:on|via|over|through) whatsapp$", t, re.I) \
        or re.match(r"^open (?:my )?whatsapp chat (?:with|of) (?P<who>[\w .']+)$", t, re.I)
    if m:
        return {"action": "whatsapp", "contact": m.group("who").strip(), "message": (m.groupdict().get("msg") or "").strip() or None}
    # ---- navigation
    m = re.match(r"^(?:navigate|start navigation|start navigating|get directions|directions|take me|drive(?: me)?|guide me|route)(?: me)? to (?P<q>.+)$", low) \
        or re.match(r"^open (?:google )?maps? (?:to|for) (?P<q>.+)$", low) or re.match(r"^show (?P<q>.+) on (?:the |google )?maps?$", low)
    if m and not re.match(r"^(?:how long|how far)", low):
        return {"action": "navigate", "query": t[m.start("q"):m.end("q")]}
    # ---- play / search in a media app
    m = re.match(r"^(?:play|put on|listen to|watch|search)(?: me)?(?: some| a| the)? (?P<q>.+?) (?:on|in|using|with|from|via) (?P<app>youtube music|yt music|youtube|you tube|yt|spotify)$", low) \
        or re.match(r"^(?:search|look) (?:on |in )?(?P<app>youtube music|youtube|yt|spotify) for (?P<q>.+)$", low) \
        or re.match(r"^(?:find|search for|look up) (?P<q>.+?) (?:on|in) (?P<app>youtube music|youtube|yt|spotify)$", low) \
        or re.match(r"^(?P<app>youtube|spotify) (?P<q>.+)$", low)
    if m:
        return {"action": "play", "query": t[m.start("q"):m.end("q")].strip(), "app": PLAY_APPS[m.group("app")]}
    m = re.match(r"^(?:play|put on|listen to)(?: me)?(?: some| a| the)? (?P<q>.+)$", low)
    if m and not re.match(r"^(?:music|song|video|it|playback)$", m.group("q")):
        video = bool(re.search(r"\b(video|trailer|episode|lecture|tutorial|movie|clip|highlights|vlog|podcast)\b", m.group("q")))
        return {"action": "play", "query": t[m.start("q"):m.end("q")].strip(), "app": "youtube" if video else None}
    # ---- websites
    m = re.match(r"^(?:open|go to|visit|launch|browse(?: to)?|take me to)(?: the)?(?: website| site| page)? (?P<u>\S+)$", low)
    if m and SITE_RX.match(m.group("u")):
        return {"action": "open_url", "query": m.group("u")}
    m = re.match(r"^google (?P<q>.+)$", low) or re.match(r"^search google for (?P<q>.+)$", low) or re.match(r"^open google (?:and )?search(?: for)? (?P<q>.+)$", low)
    if m:
        return {"action": "open_url", "query": "https://www.google.com/search?q=" + urllib.parse.quote_plus(t[m.start("q"):m.end("q")])}
    return None


def _enc(q: str) -> str:
    return urllib.parse.quote_plus(q.strip()[:150])


class AppsAgent(BaseAgent):
    """Opens things on the phone. It never operates inside another app: you press play, send or start."""
    name = "apps"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd = parse_apps(text)
        if cmd is None:
            try:
                cmd = self.extract(AppsOut, APPS, text, ctx, task="apps").model_dump()
            except AgentError:
                cmd = {"action": "unknown"}
        a = cmd.get("action")
        if a == "volume":
            return self._volume(cmd, ctx)
        if a == "media":
            ctl = cmd.get("control") or "play"
            out = self.propose("device.media", {"control": ctl}, ctx)
            word = {"play": "Resuming.", "pause": "Paused.", "next": "Skipping.", "previous": "Going back.", "stop": "Stopped."}.get(ctl, "Done.")
            return AgentReply(self.name, self._said(out, word), [out])
        if a == "play":
            return self._play(cmd, ctx)
        if a == "navigate":
            q = (cmd.get("query") or "").strip()
            if not q:
                return AgentReply(self.name, "Where to?", clarify="place")
            nid = self.svc.graph.resolve(q, "place")
            dest = self.svc.graph.node(nid)["label"] if nid else q
            url = f"https://www.google.com/maps/dir/?api=1&destination={_enc(dest)}&travelmode=driving"
            out = self.propose("device.open_url", {"url": url, "label": f"directions to {dest}", "app": "Maps"}, ctx)
            return AgentReply(self.name, self._said(out, f"Opening directions to {dest} in Maps. Tap Start to begin."), [out])
        if a == "open_url":
            return self._site(cmd.get("query") or "", ctx)
        if a == "whatsapp":
            return self._whatsapp(cmd, ctx)
        return AgentReply(self.name, "I can play things on YouTube or Spotify, open WhatsApp chats, start navigation, open websites "
                                     "and change the volume. What would you like?")

    def _said(self, out, done_text: str) -> str:
        if out.status == "done" and isinstance(out.data, dict):
            if out.data.get("ok") is False:
                return f"That didn't work: {out.data.get('error', 'unknown error')}."
            if out.data.get("needs_tap"):
                return ("Android won't let me open that while I'm in the background, so it's waiting in your notifications. Tap it. "
                        "To skip this next time, allow \"Open apps from the background\" in JARVIS Settings.")
        return self.say_outcome(out, done_text)

    def _volume(self, cmd, ctx) -> AgentReply:
        ctl = cmd.get("control") or "up"
        args = {"control": ctl}
        if ctl == "set":
            args["level"] = int(cmd.get("level") if cmd.get("level") is not None else 50)
        out = self.propose("device.volume", args, ctx)
        lvl = out.data.get("level") if out.status == "done" and isinstance(out.data, dict) else None
        word = {"up": "Volume up", "down": "Volume down", "mute": "Muted", "unmute": "Unmuted", "max": "Volume at maximum",
                "set": f"Volume set to {args.get('level')}%"}.get(ctl, "Done")
        if lvl is not None and ctl in ("up", "down"):
            word += f", now {lvl}%"
        return AgentReply(self.name, self._said(out, word + "."), [out])

    def _play(self, cmd, ctx) -> AgentReply:
        q = (cmd.get("query") or "").strip()
        if not q:
            return AgentReply(self.name, "What should I play?", clarify="content")
        app = cmd.get("app") or self.svc.db.kv_get("music_app") or "youtube"
        app = PLAY_APPS.get(app, app)
        if app == "spotify":
            url, name = f"https://open.spotify.com/search/{urllib.parse.quote(q[:150])}", "Spotify"
        elif app == "ytmusic":
            url, name = f"https://music.youtube.com/search?q={_enc(q)}", "YouTube Music"
        else:
            url, name = f"https://www.youtube.com/results?search_query={_enc(q)}", "YouTube"
        out = self.propose("device.open_url", {"url": url, "label": f"{q} on {name}", "app": name}, ctx)
        return AgentReply(self.name, self._said(out, f"Opening {q} on {name}. Tap the one you want."), [out])

    def _site(self, raw: str, ctx) -> AgentReply:
        raw = raw.strip()
        if raw.startswith("https://www.google.com/search?q="):
            url, label = raw, "Google search"
        else:
            m = SITE_RX.match(raw)
            if not m:
                return AgentReply(self.name, "Which website?", clarify="content")
            url, label = f"https://{m.group('host').lower()}{m.group('path') or ''}", m.group("host").lower()
        if is_blocked_app(url):
            return AgentReply(self.name, "I don't open payment, banking or password sites.")
        out = self.propose("device.open_url", {"url": url, "label": label, "app": "browser"}, ctx)
        return AgentReply(self.name, self._said(out, f"Opening {label}."), [out])

    def _whatsapp(self, cmd, ctx) -> AgentReply:
        who = cmd.get("contact") or ctx.focus.get("last_person")
        if not who:
            return AgentReply(self.name, "Who should I message on WhatsApp?", clarify="contact")
        c, perm = self.svc.lookup_contact(who, self.name, resume=ctx.original or None, need="phone")
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if not c or not c.get("phone"):
            return AgentReply(self.name, f"I couldn't find a phone number for {who}. Tell me the number, or save it in your contacts.")
        digits = re.sub(r"\D", "", c["phone"])
        if len(digits) == 10:
            digits = (self.svc.db.kv_get("country_code") or "91") + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = (self.svc.db.kv_get("country_code") or "91") + digits[1:]
        if not (11 <= len(digits) <= 15):
            return AgentReply(self.name, f"The number I have for {c['name']} doesn't look complete.")
        msg = (cmd.get("message") or "").strip()
        if not msg:
            out = self.propose("device.open_url", {"url": f"https://wa.me/{digits}", "label": f"WhatsApp chat with {c['name']}", "app": "WhatsApp"}, ctx)
            return AgentReply(self.name, self._said(out, f"Opening your WhatsApp chat with {c['name']}."), [out], focus={"last_person": c["name"]})
        msg = msg[0].upper() + msg[1:]
        out = self.propose("whatsapp.compose", {"number": digits, "name": c["name"], "text": msg}, ctx)
        return AgentReply(self.name, self._said(out, f"WhatsApp is open with your message to {c['name']}. Tap send."), [out],
                          focus={"last_person": c["name"]})
