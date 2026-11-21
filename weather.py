"""Weather for TooGee, from Open-Meteo (https://open-meteo.com): free, no API key.

Two calls: the geocoding API turns a place name into coordinates (results are
ranked by population, so "Portland" is Oregon; "Portland, Maine" or "Portland
ME" picks the other one), and the forecast API returns current conditions plus
hourly and daily forecasts in the place's own time zone. Both are cached
briefly, so follow-up questions are instant.

Only the place name you ask about (or your WILI_LOCATION) and its coordinates
are sent to Open-Meteo.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_TTL = 600  # seconds

# WMO weather interpretation codes (Open-Meteo docs)
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}
SNOW_CODES = {71, 73, 75, 77, 85, 86}

US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts",
    "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


@dataclass
class Place:
    name: str
    region: str      # state / province
    country: str
    country_code: str
    lat: float
    lon: float

    def spoken(self) -> str:
        """'Portland, Oregon' -- region for clarity, country only if foreign."""
        parts = [self.name]
        if self.region and self.region != self.name:
            parts.append(self.region)
        if self.country_code and self.country_code != "US":
            parts.append(self.country)
        return ", ".join(parts)


class Weather:
    def __init__(self, units: str = "imperial", home: str = "") -> None:
        self.imperial = units.lower() != "metric"
        self.home = home.strip()
        self.s = requests.Session()
        self._places: dict[str, Place | None] = {}
        self._forecasts: dict[tuple[float, float], tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lookup
    def geocode(self, query: str) -> Place | None:
        key = query.lower().strip()
        with self._lock:
            if key in self._places:
                return self._places[key]
        place = self._geocode(query)
        with self._lock:
            self._places[key] = place
        return place

    def _search(self, name: str) -> list[dict]:
        r = self.s.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en",
                                           "format": "json"}, timeout=10)
        r.raise_for_status()
        return r.json().get("results") or []

    def _geocode(self, query: str) -> Place | None:
        q = re.sub(r"\s+", " ", query.strip().strip(".?!"))
        # "Portland, Maine" / "Portland, ME" / "Paris France" / "Portland Maine"
        if "," in q:
            name, qualifier = [p.strip() for p in q.split(",", 1)]
            candidates = [(name, qualifier)]
        else:
            # Try "city + state/country" readings first: they only match when the
            # qualifier really is the region or country ("Portland ME"), so they
            # can't misfire -- whereas the whole string as a name once turned
            # "Portland ME" into "Portland Meadows". Whole string last.
            words = q.split()
            candidates = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(len(words) - 1, 0, -1)]
            candidates.append((q, ""))
        for name, qualifier in candidates:
            results = self._search(name)
            if qualifier:
                want = qualifier.lower()
                want = US_STATES.get(want, want)
                results = [r for r in results if want in (r.get("admin1") or "").lower()
                           or want in (r.get("country") or "").lower()
                           or want == (r.get("country_code") or "").lower()]
            if results:
                r = results[0]  # already ranked by population
                return Place(r["name"], r.get("admin1") or "", r.get("country") or "",
                             r.get("country_code") or "", r["latitude"], r["longitude"])
        return None

    def forecast(self, place: Place) -> dict:
        key = (round(place.lat, 3), round(place.lon, 3))
        with self._lock:
            hit = self._forecasts.get(key)
            if hit and time.time() - hit[0] < FORECAST_TTL:
                return hit[1]
        params = {
            "latitude": place.lat, "longitude": place.lon, "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                       "wind_speed_10m,is_day",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,precipitation_sum,sunrise,sunset,wind_speed_10m_max",
        }
        if self.imperial:
            params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
        r = self.s.get(FORECAST_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        with self._lock:
            self._forecasts[key] = (time.time(), data)
        return data

    # ---------------------------------------------------------------- wording
    @property
    def deg(self) -> str:
        return "°F" if self.imperial else "°C"

    @property
    def wind(self) -> str:
        return "mph" if self.imperial else "km/h"

    def resolve(self, location: str | None) -> tuple[Place | None, str]:
        """(place, error). Uses the home location when none is given."""
        loc = (location or "").strip()
        if not loc or loc.lower() in ("here", "home", "my location", "outside", "local"):
            if not self.home:
                return None, ("No home location is set, so ask the user which city they mean. "
                              "(They can set a default with WILI_LOCATION.)")
            loc = self.home
        try:
            place = self.geocode(loc)
        except requests.RequestException as exc:
            return None, f"The weather service didn't answer ({type(exc).__name__})."
        if not place:
            return None, f"I couldn't find a place called {loc!r}."
        return place, ""

    def report(self, location: str | None = None, days: int = 3) -> str:
        """Detailed text for the model: now, the next 12 hours, and `days` days."""
        place, err = self.resolve(location)
        if not place:
            return err
        try:
            f = self.forecast(place)
        except requests.RequestException as exc:
            return f"The weather service didn't answer ({type(exc).__name__})."
        c, d, h = f["current"], f["daily"], f["hourly"]
        lines = [f"Weather for {place.spoken()} (local time there {_clock(c['time'])}, "
                 f"{f.get('timezone_abbreviation', '')}). Units: {self.deg}, {self.wind}."]
        lines.append(f"Now: {c['temperature_2m']:.0f}{self.deg} (feels like {c['apparent_temperature']:.0f}), "
                     f"{WMO.get(c['weather_code'], 'unknown')}, humidity {c['relative_humidity_2m']}%, "
                     f"wind {c['wind_speed_10m']:.0f} {self.wind}.")
        # next 12 hours, every 3 hours
        try:
            start = h["time"].index(c["time"][:13] + ":00")
        except ValueError:
            start = 0
        hours = []
        for i in range(start + 3, min(start + 13, len(h["time"])), 3):
            hours.append(f"{_clock(h['time'][i])} {h['temperature_2m'][i]:.0f}°, "
                         f"{WMO.get(h['weather_code'][i], '?')}, {h['precipitation_probability'][i]}% precip")
        if hours:
            lines.append("Next hours: " + "; ".join(hours) + ".")
        days = max(1, min(7, int(days or 3)))
        for i in range(min(days, len(d["time"]))):
            label = "Today" if i == 0 else ("Tomorrow" if i == 1 else _weekday(d["time"][i]))
            lines.append(
                f"{label} ({d['time'][i]}): high {d['temperature_2m_max'][i]:.0f}, "
                f"low {d['temperature_2m_min'][i]:.0f}, {WMO.get(d['weather_code'][i], 'unknown')}, "
                f"{d['precipitation_probability_max'][i]}% chance of precipitation"
                f" ({d['precipitation_sum'][i]} {'in' if self.imperial else 'mm'}), "
                f"wind up to {d['wind_speed_10m_max'][i]:.0f} {self.wind}"
                + (f", sunrise {_clock(d['sunrise'][i])}, sunset {_clock(d['sunset'][i])}" if i == 0 else "")
                + ".")
        return "\n".join(lines)

    def summary(self, location: str | None = None) -> str:
        """One spoken sentence or two for "what's the weather?"."""
        place, err = self.resolve(location)
        if not place:
            return err
        f = self.forecast(place)
        c, d = f["current"], f["daily"]
        code = c["weather_code"]
        cond = WMO.get(code, "")
        s = f"It's {c['temperature_2m']:.0f} degrees and {cond} in {place.spoken()}"
        feels = c["apparent_temperature"]
        if abs(feels - c["temperature_2m"]) >= 5:
            s += f", feeling like {feels:.0f}"
        s += f". Today's high is {d['temperature_2m_max'][0]:.0f} with a low of {d['temperature_2m_min'][0]:.0f}"
        p = d["precipitation_probability_max"][0] or 0
        if p >= 20:
            kind = "snow" if d["weather_code"][0] in SNOW_CODES else "rain"
            s += f", and a {p} percent chance of {kind}"
        return s + "."


def _clock(iso: str) -> str:
    """'2026-11-21T16:34' -> '4:34 PM' (already local to the place)."""
    t = time.strptime(iso[:16], "%Y-%m-%dT%H:%M")
    return time.strftime("%-I:%M %p", t).replace(":00 ", " ")


def _weekday(iso_date: str) -> str:
    return time.strftime("%A", time.strptime(iso_date, "%Y-%m-%d"))


# --------------------------------------------------------------------------- the instant path
_WEATHER_Q = re.compile(
    r"^(?:hey |ok |okay |so )?(?:what s|what is|whats|how s|how is|hows|tell me|give me|check)"
    r"(?: the| today s| todays| current)? (?:weather|forecast)(?: like)?"
    r"(?: (?:today|right now|now|outside|out there|currently))?"
    r"(?: (?:in|for|at|over) (?P<place>[a-z][a-z .']*?))?"
    r"(?: (?:today|right now|now|currently))?$"
)


def parse_weather_question(text: str) -> tuple[bool, str] | None:
    """'What's the weather?' / 'How's the weather in Portland, Maine today?'
    -> (True, place or ''). Anything about later or specifics ('tomorrow',
    'this weekend', 'will it rain', 'do I need a jacket') is left to the
    model, which has the full forecast."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9 ,.']+", " ", t).replace("'", " ").replace(",", " ")
    t = re.sub(r"\s+", " ", t).strip(" .")
    if re.search(r"\b(tomorrow|tonight|weekend|week|monday|tuesday|wednesday|thursday|friday|"
                 r"saturday|sunday|later|hour|morning|afternoon|evening|rain|snow|umbrella|"
                 r"jacket|coat|wind|humid|sunset|sunrise)\b", t):
        return None
    m = _WEATHER_Q.match(t)
    if not m:
        return None
    return True, (m.group("place") or "").strip()
