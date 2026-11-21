"""Timers and reminders for TooGee.

An Alarm is either a timer ("5 minutes") or a reminder ("take the trash out",
at 7 pm). Alarms are kept in the assistant's state file, so they survive a
restart; anything that came due while TooGee was off is announced when it comes
back, if it's less than MISSED_MAX_SEC old.

This module is pure logic plus a synthesised bell; the assistant decides when
to ring and how to talk about it.
"""

from __future__ import annotations

import math
import re
import struct
import threading
import time
from dataclasses import asdict, dataclass, field

MISSED_MAX_SEC = 6 * 3600


@dataclass
class Alarm:
    id: int
    kind: str            # "timer" or "reminder"
    due: float           # epoch seconds
    label: str = ""      # timer name ("pasta") or reminder text ("take the trash out")
    duration: int = 0    # timers: the length that was asked for, in seconds
    created: float = field(default_factory=time.time)
    missed: bool = False  # came due while TooGee wasn't running

    def describe(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        left = self.due - now
        if self.kind == "timer":
            name = f"{self.label} timer" if self.label else f"{say_duration(self.duration, adjective=True)} timer"
            return f"{name}, {say_duration(left)} left" if left > 0 else f"{name}, done"
        return f"reminder to {_lower_first(self.label)} at {say_clock(self.due, now)}"

    def announcement(self) -> str:
        if self.kind == "timer":
            if self.label:
                return f"Your {self.label} timer is done!"
            return f"Your {say_duration(self.duration, adjective=True)} timer is done!"
        lead = "While I was off, you had a reminder: " if self.missed else "Reminder: "
        return lead + self.label.rstrip(".") + "."


class Alarms:
    """Thread-safe list of pending alarms, saved through `save(list_of_dicts)`."""

    def __init__(self, saved: list[dict] | None, save) -> None:
        self._lock = threading.Lock()
        self._save = save
        self._items: list[Alarm] = []
        now = time.time()
        for d in saved or []:
            try:
                a = Alarm(**{k: d[k] for k in Alarm.__dataclass_fields__ if k in d})
            except (TypeError, KeyError):
                continue
            if a.due < now:
                if now - a.due > MISSED_MAX_SEC:
                    continue          # too stale to be useful
                a.missed = True
            self._items.append(a)
        self._next_id = max((a.id for a in self._items), default=0) + 1

    def _persist(self) -> None:
        self._save([asdict(a) for a in self._items])

    def add_timer(self, seconds: float, label: str = "", duration: int | None = None) -> Alarm:
        """`duration` is the length to announce ("your 5 minute timer"), if it
        differs from `seconds` -- e.g. when a timer is snoozed."""
        seconds = max(1, int(round(seconds)))
        with self._lock:
            a = Alarm(self._next_id, "timer", time.time() + seconds, label.strip(), duration or seconds)
            self._next_id += 1
            self._items.append(a)
            self._persist()
        return a

    def add_reminder(self, due: float, text: str) -> Alarm:
        with self._lock:
            a = Alarm(self._next_id, "reminder", due, text.strip())
            self._next_id += 1
            self._items.append(a)
            self._persist()
        return a

    def pending(self) -> list[Alarm]:
        with self._lock:
            return sorted(self._items, key=lambda a: a.due)

    def due(self, now: float | None = None) -> Alarm | None:
        """The earliest alarm that's due, removed from the list (or None)."""
        now = time.time() if now is None else now
        with self._lock:
            ready = [a for a in self._items if a.due <= now]
            if not ready:
                return None
            a = min(ready, key=lambda x: x.due)
            self._items.remove(a)
            self._persist()
            return a

    def cancel(self, match: str = "", everything: bool = False, kind: str = "") -> list[Alarm]:
        """Cancel alarms. With `everything`, all (of `kind`, if given). Otherwise
        those whose label contains `match`; with no match and exactly one
        candidate, that one."""
        m = match.lower().strip()
        with self._lock:
            pool = [a for a in self._items if not kind or a.kind == kind]
            if everything:
                gone = pool
            elif m:
                gone = [a for a in pool if m in a.label.lower()
                        or (a.kind == "timer" and m in say_duration(a.duration))]
            else:
                gone = pool if len(pool) == 1 else []
            for a in gone:
                self._items.remove(a)
            if gone:
                self._persist()
            return gone


# --------------------------------------------------------------------------- speaking times
def _lower_first(text: str) -> str:
    """'Take the trash out' -> 'take the trash out', but leave 'NASA ...' alone."""
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def say_duration(seconds: float, adjective: bool = False) -> str:
    """90 -> '1 minute and 30 seconds'; adjective=True -> '1 minute and 30 second'
    (as in 'your 10 minute timer')."""
    s = max(0, int(round(seconds)))

    def unit(n: int, word: str) -> str:
        return f"{n} {word}{'' if adjective or n == 1 else 's'}"

    if s < 60:
        return unit(s, "second")
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(unit(h, "hour"))
    if m:
        parts.append(unit(m, "minute"))
    if sec and not h:
        parts.append(unit(sec, "second"))
    return " and ".join(parts)


def say_clock(due: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    t = time.localtime(due)
    clock = time.strftime("%-I:%M %p", t).replace(":00 ", " ")
    day = time.strftime("%Y%m%d", t)
    if day == time.strftime("%Y%m%d", time.localtime(now)):
        return clock
    if day == time.strftime("%Y%m%d", time.localtime(now + 86400)):
        return f"{clock} tomorrow"
    return time.strftime("%A %B %-d at ", t) + clock


def parse_local_time(iso: str, now: float | None = None) -> float:
    """'2026-11-21T19:00' (local, as the model writes it) -> epoch. A time a
    little in the past is assumed to mean the next day."""
    now = time.time() if now is None else now
    iso = iso.strip().replace(" ", "T")
    iso = re.sub(r"(Z|[+-]\d\d:?\d\d)$", "", iso)  # treat as local time
    fmt = "%Y-%m-%dT%H:%M:%S" if iso.count(":") == 2 else "%Y-%m-%dT%H:%M"
    due = time.mktime(time.strptime(iso, fmt))
    if due < now - 60 and due + 86400 > now:
        due += 86400
    return due


# --------------------------------------------------------------------------- the instant path
_WORDNUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORDNUM.update({"thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60,
                 "ninety": 90, "a": 1, "an": 1, "half an": 0.5, "a half": 0.5})
_UNIT = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}
_NUM = r"(\d+(?:\.\d+)?|half an|a half|forty five|[a-z]+)"


def _num(tok: str) -> float | None:
    return float(tok) if re.fullmatch(r"\d+(?:\.\d+)?", tok) else _WORDNUM.get(tok)


def parse_timer_command(text: str) -> tuple[int, str] | None:
    """'Set a timer for 5 minutes', 'ten minute pasta timer', 'timer 30 seconds'
    -> (seconds, label). Anything fancier is left to the model."""
    t = re.sub(r"[^a-z0-9. ]+", " ", text.lower().replace("-", " "))
    t = re.sub(r"\s+", " ", t).strip(" .")
    if "timer" not in t or len(t.split()) > 12 or re.search(r"\b(cancel|stop|delete|how|what|left|remove)\b", t):
        return None
    total, found = 0.0, False
    for m in re.finditer(rf"\b{_NUM} ({'|'.join(_UNIT)})s?\b", t):
        n = _num(m.group(1))
        if n is None:
            continue
        total += n * _UNIT[m.group(2)]
        found = True
    m = re.search(r"\band a half\b", t)
    if found and m:
        unit = re.findall(rf"\b({'|'.join(_UNIT)})s?\b", t)
        if unit:
            total += 0.5 * _UNIT[unit[-1]]
    if not found or total <= 0 or total > 24 * 3600:
        return None
    label = ""
    m = re.search(r"\b(?:for (?:the |my )?)?([a-z]+(?: [a-z]+)?) timer\b", t)
    if m:
        words = [w for w in m.group(1).split()
                 if w not in _UNIT and w + "s" not in _UNIT and w.rstrip("s") not in _UNIT
                 and _num(w) is None and w not in {"a", "an", "the", "set", "start", "my", "minute", "second", "hour",
                                                         "at", "to", "and", "please", "new", "another", "me", "up",
                                                         "that", "this", "is", "we", "you", "can", "could", "would",
                                                         "will", "go", "make", "put", "ll", "s"} and len(w) > 1]
        label = " ".join(words)
    return int(round(total)), label


_DISMISS_SNOOZE = re.compile(rf"\bsnooze(?: (?:it|for|me))*(?: {_NUM} (minute|min|hour|second)s?)?\b")


_SNOOZE_WORD = re.compile(r"\b(?:snooze[sd]?|snooz|snoo|snews|snus)\b")
_ANY_DURATION = re.compile(rf"\b{_NUM} (?:more |extra )?(minute|min|hour|hr|second|sec)s?\b")


def parse_alarm_reply(text: str) -> tuple[str, int] | None:
    """What the user said while an alarm rang: ('snooze', seconds),
    ('dismiss', 0), or None if nothing was said.

    Any duration in the reply means snooze for that long -- nobody says "for
    one minute" to dismiss an alarm, and it rescues "snooze" being misheard
    (it once came back as "It's news for one minute")."""
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()
    if not t:
        return None
    m = _ANY_DURATION.search(t)
    if m and _num(m.group(1)):
        return "snooze", int(_num(m.group(1)) * _UNIT[m.group(2)])
    if _SNOOZE_WORD.search(t) or _DISMISS_SNOOZE.search(t):
        return "snooze", 5 * 60
    return "dismiss", 0   # any other reply means "I heard it"


# --------------------------------------------------------------------------- the bell
def bell_pcm(rate: int = 8000, dings: int = 3, amp: int = 9000) -> bytes:
    """A friendly two-tone bell, as 16-bit mono PCM at `rate`."""
    out: list[int] = []
    for d in range(dings):
        f0 = 1046.5 if d % 2 == 0 else 1318.5   # C6 / E6
        n = int(0.42 * rate)
        for i in range(n):
            t = i / rate
            env = math.exp(-t * 7.0) * min(1.0, t / 0.004)
            s = math.sin(2 * math.pi * f0 * t) + 0.35 * math.sin(2 * math.pi * 2 * f0 * t)
            out.append(int(amp * env * s / 1.35))
        out.extend([0] * int(0.06 * rate))
    out.extend([0] * int(0.15 * rate))
    return struct.pack(f"<{len(out)}h", *out)
