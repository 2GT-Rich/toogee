"""Local time and date for TooGee, without asking the model.

"What time is it?" and "what's the date?" are the questions people ask a desk
assistant most, and the answer is on this Mac's clock, so there's no reason to
wait on the language model for them. parse_clock_question() only accepts short,
plain forms of those questions (it must match the whole request), so "what time
does Costco close?", "what day is Christmas?" or "what time is it in Tokyo?"
still go to the model.
"""

from __future__ import annotations

import re
import time

# Said before or after the question; stripped before matching.
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|hmm|excuse me|sorry|quick question|yo)\s+)*"
    r"(?:(?:can|could|would|will) you (?:please )?(?:tell me|remind me|say)\s+"
    r"|(?:do|did) you (?:happen to )?know\s+|(?:please )?tell me\s+|i (?:need|want) to know\s+"
    r"|any idea\s+|you know\s+)?")
_TRAIL = re.compile(
    r"(?:\s+(?:please|thanks|thank you|right now|now|currently|exactly|at the moment|"
    r"there|here|for me|again|today))+$")

_TIME = [
    r"what(?:s| is) (?:the )?(?:current |exact )?time(?: is it)?",
    r"what time is it",
    r"what time(?: it)? is(?: it)?",       # "tell me what time it is"
    r"(?:the )?(?:current )?time",          # "tell me the time"
    r"(?:have you )?got the time",
    r"time check",
    r"how late is it",
]
_DATE = [
    r"what(?:s| is) (?:the |todays |the current )?date",
    r"what date is it",
    r"what(?: is)? (?:the )?date(?: it)? is(?: it)?",
    r"(?:the |todays )?date",
    r"what(?:s| is) todays? date",
]
_DAY = [
    r"what day(?: of the week)? is (?:it|today)",
    r"what(?:s| is) (?:the )?day(?: of the week)?",
    r"what day(?: of the week)? it is",
    r"what(?:s| is) today",
    r"what day",
]
_MONTH = [r"what month is it", r"what(?:s| is) the month", r"what month it is"]
_YEAR = [r"what year is it", r"what(?:s| is) the year", r"what year it is"]


def _any(forms: list[str]) -> str:
    return "(?:" + "|".join(forms) + ")"


_KINDS = [
    ("both", re.compile(rf"(?:{_any(_TIME)}|time) and (?:the )?date|"
                        rf"(?:{_any(_DATE)}|date) and (?:the )?time")),
    ("time", re.compile(_any(_TIME))),
    ("date", re.compile(_any(_DATE))),
    ("day", re.compile(_any(_DAY))),
    ("month", re.compile(_any(_MONTH))),
    ("year", re.compile(_any(_YEAR))),
]


def _normalize(text: str) -> tuple[str, str]:
    """(with the trailing 'please', 'today' etc. removed, without removing them)
    -- 'what day is today' needs its 'today'."""
    t = text.lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEAD.sub("", t)
    return _TRAIL.sub("", t).strip(), t


def parse_clock_question(text: str) -> str | None:
    """'What time is it?' -> 'time'; also 'date', 'day', 'month', 'year' and
    'both' (time and date). None for anything else."""
    short, full = _normalize(text)
    if not full or len(full.split()) > 9:
        return None
    for kind, rx in _KINDS:
        if (short and rx.fullmatch(short)) or rx.fullmatch(full):
            return kind
    return None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def say_time(t: time.struct_time) -> str:
    """'3:05 PM', '7 PM', 'noon', 'midnight'."""
    if t.tm_min == 0 and t.tm_hour == 12:
        return "noon"
    if t.tm_min == 0 and t.tm_hour == 0:
        return "midnight"
    return time.strftime("%-I:%M %p", t).replace(":00 ", " ")


def say_date(t: time.struct_time) -> str:
    """'Saturday, November 21st'."""
    return f"{time.strftime('%A, %B', t)} {_ordinal(t.tm_mday)}"


def answer(kind: str, now: float | None = None) -> str:
    t = time.localtime(time.time() if now is None else now)
    if kind == "time":
        return f"It's {say_time(t)}."
    if kind == "both":
        return f"It's {say_time(t)} on {say_date(t)}."
    if kind in ("date", "day"):
        return f"Today is {say_date(t)}."
    if kind == "month":
        return f"It's {time.strftime('%B', t)}."
    if kind == "year":
        return f"It's {t.tm_year}."
    raise ValueError(kind)
