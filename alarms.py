"""Timers, reminders and alarms for TooGee.

An Alarm is a timer ("5 minutes"), a reminder ("take the trash out", at 7 pm)
or an alarm clock (7 AM, once or repeating: every day, weekdays, weekends or
given days). They're kept in the assistant's state file, so they survive a
restart. A timer or reminder that came due while TooGee was off is announced
when it comes back, if it's less than MISSED_MAX_SEC old; an alarm only if
it's less than ALARM_LATE_SEC late (nobody wants a wake-up call at noon), and a
repeating one is simply moved on to its next day.

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
ALARM_LATE_SEC = 15 * 60

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]          # time.localtime().tm_wday order
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
REPEATS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}


def repeat_days(repeat: str) -> list[int]:
    """'weekdays' -> [0..4]; 'mon,wed' -> [0, 2]; '' -> [] (once)."""
    r = repeat.lower().strip()
    names = REPEATS.get(r) or [d.strip()[:3] for d in r.split(",") if d.strip()]
    return sorted({DAYS.index(d) for d in names if d in DAYS})


def normalize_repeat(repeat: str) -> str:
    """Canonical form: '', 'daily', 'weekdays', 'weekends' or 'mon,wed,fri'."""
    days = repeat_days(repeat)
    for name, ds in REPEATS.items():
        if days == [DAYS.index(d) for d in ds]:
            return name
    return ",".join(DAYS[d] for d in days)


def next_at(clock: str, repeat: str = "", after: float | None = None, date: str = "") -> float:
    """The next time the clock reads `clock` ("07:30") after `after`: on one
    of the repeat days, on `date` ("YYYY-MM-DD") if given, else the first."""
    after = time.time() if after is None else after
    hh, mm = (int(x) for x in clock.split(":"))
    if date:
        y, mo, d = (int(x) for x in date.split("-"))
        return time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))
    days = repeat_days(repeat)
    base = time.localtime(after)
    for i in range(8):
        t = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + i, hh, mm, 0, 0, 0, -1))
        if t > after and (not days or time.localtime(t).tm_wday in days):
            return t
    raise ValueError(f"no next time for {clock} {repeat}")


def say_hhmm(clock: str) -> str:
    """'07:00' -> '7 AM', '18:30' -> '6:30 PM', '12:00' -> 'noon'."""
    hh, mm = (int(x) for x in clock.split(":"))
    if (hh, mm) == (12, 0):
        return "noon"
    if (hh, mm) == (0, 0):
        return "midnight"
    h12 = hh % 12 or 12
    return f"{h12}{f':{mm:02d}' if mm else ''} {'AM' if hh < 12 else 'PM'}"


def say_repeat(repeat: str) -> str:
    r = normalize_repeat(repeat)
    if not r:
        return ""
    if r == "daily":
        return "every day"
    if r in ("weekdays", "weekends"):
        return f"on {r}"
    names = [DAY_NAMES[DAYS.index(d)] + "s" for d in r.split(",")]
    return "on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


@dataclass
class Alarm:
    id: int
    kind: str            # "timer", "reminder" or "alarm"
    due: float           # epoch seconds
    label: str = ""      # timer name ("pasta"), reminder text ("take the trash out"), alarm note ("gym")
    duration: int = 0    # timers: the length that was asked for, in seconds
    created: float = field(default_factory=time.time)
    missed: bool = False  # came due while TooGee wasn't running
    clock: str = ""      # alarms: "07:30", local time
    repeat: str = ""     # alarms: "", "daily", "weekdays", "weekends" or "mon,wed,fri"
    snoozed: bool = False  # alarms: this is a snooze of one

    def describe(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        left = self.due - now
        if self.kind == "timer":
            name = f"{self.label} timer" if self.label else f"{say_duration(self.duration, adjective=True)} timer"
            return f"{name}, {say_duration(left)} left" if left > 0 else f"{name}, done"
        if self.kind == "alarm":
            out = f"{say_hhmm(self.clock)} alarm" + (f" for {self.label}" if self.label else "")
            if self.repeat:
                return f"{out} {say_repeat(self.repeat)} (next {say_clock(self.due, now)})"
            if self.snoozed:
                return f"{out}, snoozed until {say_clock(self.due, now)}"
            return f"{out}, {say_day(self.due, now)}"
        return f"reminder to {_lower_first(self.label)} at {say_clock(self.due, now)}"

    def announcement(self) -> str:
        if self.kind == "timer":
            if self.label:
                return f"Your {self.label} timer is done!"
            return f"Your {say_duration(self.duration, adjective=True)} timer is done!"
        if self.kind == "alarm":
            now = say_hhmm(time.strftime("%H:%M", time.localtime(self.due)))
            if self.label:
                return f"It's {now}. Time for {_lower_first(self.label).rstrip('.')}!"
            return f"It's {now}. Time to get up!" if time.localtime(self.due).tm_hour < 11 \
                else f"It's {now}. This is your alarm."
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
                if a.kind == "alarm" and now - a.due > ALARM_LATE_SEC:
                    if not a.repeat:
                        continue      # a wake-up call hours late is no use
                    a.due = next_at(a.clock, a.repeat, now)
                elif now - a.due > MISSED_MAX_SEC:
                    continue          # too stale to be useful
                elif a.kind != "alarm":
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

    def add_alarm(self, clock: str, repeat: str = "", label: str = "", date: str = "") -> Alarm:
        """An alarm at `clock` ("07:30"), once (the next time, or on `date`) or
        repeating."""
        hh, mm = (int(x) for x in clock.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError(f"bad time {clock!r}")
        clock = f"{hh:02d}:{mm:02d}"
        repeat = "" if date else normalize_repeat(repeat)
        due = next_at(clock, repeat, date=date)
        if due <= time.time():
            raise ValueError(f"{date} {clock} is in the past")
        with self._lock:
            a = Alarm(self._next_id, "alarm", due, label.strip(), clock=clock, repeat=repeat)
            self._next_id += 1
            self._items.append(a)
            self._persist()
        return a

    def snooze_alarm(self, alarm: Alarm, seconds: float) -> Alarm:
        with self._lock:
            a = Alarm(self._next_id, "alarm", time.time() + seconds, alarm.label,
                      clock=alarm.clock, snoozed=True)
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
            if a.kind == "alarm" and a.repeat:   # it'll ring again on its next day
                self._items.append(Alarm(a.id, "alarm", next_at(a.clock, a.repeat, max(now, a.due)),
                                         a.label, clock=a.clock, repeat=a.repeat))
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
                        or (a.kind == "timer" and m in say_duration(a.duration))
                        or (a.kind == "alarm" and _same_clock(m, a.clock))]
            else:
                gone = pool if len(pool) == 1 else []
            for a in gone:
                self._items.remove(a)
            if gone:
                self._persist()
            return gone


def _same_clock(said: str, clock: str) -> bool:
    """Does '7 am', '7:30', '07:30' or '7' name the alarm at `clock`?"""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d\d))?\s*(am|pm|a\.m\.|p\.m\.)?\s*", said.lower())
    if not m:
        return said.lower() in say_hhmm(clock).lower()
    hh, mm = (int(x) for x in clock.split(":"))
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if mi != mm:
        return False
    if ap:
        return (h % 12) + (12 if ap == "pm" else 0) == hh
    return h in (hh, hh % 12, (hh % 12) or 12)


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


def say_day(due: float, now: float | None = None) -> str:
    """'today', 'tonight', 'tomorrow' or 'on Monday, November 23'."""
    now = time.time() if now is None else now
    t = time.localtime(due)
    day = time.strftime("%Y%m%d", t)
    if day == time.strftime("%Y%m%d", time.localtime(now)):
        return "tonight" if t.tm_hour >= 18 else "today"
    if day == time.strftime("%Y%m%d", time.localtime(now + 86400)):
        return "tomorrow"
    return time.strftime("on %A, %B %-d", t)


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


_HOURWORD = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_MINWORD = {"oclock": 0, "o clock": 0, "oh five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
            "twenty five": 25, "thirty": 30, "thirty five": 35, "forty": 40, "forty five": 45,
            "fifty": 50, "fifty five": 55}
_AMPM = r"(a ?m|p ?m)"
_ALARM_NO = re.compile(r"\b(?:cancel|stop|delete|remove|turn off|switch off|disable|clear|what|whats|when|which|"
                       r"list|how|snooze|change|move|reset|any|do i have|is my|are my)\b")
_DAYWORD = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


def _clean(text: str) -> str:
    t = text.lower().replace("’", "'").replace("'", "").replace("-", " ")
    t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", t)          # a.m. -> am
    t = re.sub(r"(\d)[.:](\d\d)\b", r"\1:\2", t)          # 7.30 -> 7:30
    t = re.sub(r"[^a-z0-9: ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _find_time(t: str) -> tuple[int, int, str] | None:
    """(hour, minute, 'am'/'pm'/'') from 'seven thirty am', '6:45', 'half past six', 'noon'."""
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return 12, 0, "pm"
    if re.search(r"\bmidnight\b", t):
        return 0, 0, "am"
    hw = "|".join(sorted(_HOURWORD, key=len, reverse=True))
    m = re.search(rf"\b(half|quarter) (past|after|to|til|till) (\d{{1,2}}|{hw})\b(?: {_AMPM}\b)?", t)
    if m:
        h = int(m.group(3)) if m.group(3).isdigit() else _HOURWORD[m.group(3)]
        mins = 30 if m.group(1) == "half" else 15
        if m.group(2) in ("to", "til", "till"):
            h, mins = (h - 1) % 24, 60 - mins
        return h, mins, (m.group(4) or "").replace(" ", "")
    # Speech recognition writes "3:45 pm" as "345 pm": run-together digits are
    # a time when am/pm or "for"/"at" says so (not a bare "2024").
    m = re.search(rf"\b(\d{{1,2}})([0-5]\d)(?!\d)(?: ?{_AMPM}\b)?", t)
    if m and (m.group(3) or re.search(rf"\b(?:for|at|to) {m.group(1)}{m.group(2)}\b", t)):
        return int(m.group(1)), int(m.group(2)), (m.group(3) or "").replace(" ", "")
    m = re.search(rf"\b(\d{{1,2}})(?::(\d\d)| (\d\d)\b)?(?: ?{_AMPM}\b)?", t)
    if m and (m.group(2) or m.group(3) or m.group(4) or re.search(rf"\b(?:for|at|to) {m.group(1)}\b", t)):
        h, mins = int(m.group(1)), int(m.group(2) or m.group(3) or 0)
        return h, mins, (m.group(4) or "").replace(" ", "")
    mw = "|".join(sorted(_MINWORD, key=len, reverse=True))
    m = re.search(rf"\b({hw})(?: ({mw}))?(?: {_AMPM})?\b", t)
    if m and _HOURWORD[m.group(1)] > 0 and (m.group(2) or m.group(3)
                                             or re.search(rf"\b(?:for|at|to) {m.group(1)}\b", t)):
        return _HOURWORD[m.group(1)], _MINWORD.get(m.group(2) or "", 0), (m.group(3) or "").replace(" ", "")
    return None


def parse_alarm_command(text: str, now: float | None = None) -> tuple[str, str, str] | None:
    """'Set an alarm for 7 am', 'wake me up at six thirty tomorrow', 'alarm for
    6:45 every weekday' -> (clock 'HH:MM', repeat, date 'YYYY-MM-DD' or '').
    Anything fancier (or a question about alarms) is left to the model.

    Without am/pm: a repeating alarm, or one for a given day, is in the
    morning; otherwise it's whichever of the two comes first. TooGee says
    which it set, so a wrong guess is easy to catch."""
    now = time.time() if now is None else now
    t = _clean(text)
    if not re.search(r"\balarm\b|\bwake me\b|\bwake us\b", t) or len(t.split()) > 16 or _ALARM_NO.search(t):
        return None
    if re.search(r"\b(?:in|for) (?:\d+|[a-z]+) (?:minute|min|hour|hr|second)s?\b", t):
        return None                     # "an alarm in 20 minutes" is a timer: the model knows
    found = _find_time(t)
    if not found:
        return None
    h, mins, ap = found
    if not ap:
        if re.search(r"\b(?:in the morning|this morning|morning)\b", t):
            ap = "am"
        elif re.search(r"\b(?:tonight|this evening|in the evening|in the afternoon|this afternoon|evening|afternoon)\b", t):
            ap = "pm"
    if h > 23 or mins > 59 or (ap and not 1 <= h <= 12):
        return None
    if ap:
        h = h % 12 + (12 if ap == "pm" else 0)

    repeat = ""
    if re.search(r"\b(?:every ?day|daily|every morning|every night|each day|each morning)\b", t):
        repeat = "daily"
    elif re.search(r"\bweekdays?\b|\bwork ?days?\b", t):
        repeat = "weekdays"
    elif re.search(r"\bweekends?\b", t):
        repeat = "weekends"
    days = [d for d, i in _DAYWORD.items() if re.search(rf"\b{d}s?\b", t)]
    date = ""
    if days and not repeat:
        if re.search(r"\bevery\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)days\b", t):
            repeat = ",".join(DAYS[_DAYWORD[d]] for d in days)
        else:   # "on Monday": the next one
            lt = time.localtime(now)
            ahead = (_DAYWORD[days[0]] - lt.tm_wday) % 7 or 7
            date = time.strftime("%Y-%m-%d", time.localtime(time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + ahead, 12, 0, 0, 0, 0, -1))))
    if re.search(r"\btomorrow\b", t) and not repeat:
        date = time.strftime("%Y-%m-%d", time.localtime(now + 86400))

    if not ap and h <= 12:
        if repeat or date:
            h = h % 12 if h < 12 else 12          # morning (12 -> noon)
        else:
            first = min((next_at(f"{hh:02d}:{mins:02d}", after=now), hh) for hh in {h % 12, h % 12 + 12})
            h = first[1]
    return f"{h:02d}:{mins:02d}", repeat, date


def parse_alarm_cancel(text: str) -> tuple[str, bool] | None:
    """'Cancel my alarm', 'turn off the 7 am alarm', 'delete all my alarms'
    -> (time said, or '', all?)."""
    t = _clean(text)
    if not re.search(r"\balarms?\b", t) or len(t.split()) > 10:
        return None
    if not re.search(r"\b(?:cancel|delete|remove|turn off|switch off|disable|clear|get rid of)\b", t):
        return None
    if re.search(r"\b(?:timer|reminder|tomorrow|today|skip|just|only|next)\b", t):
        return None                     # e.g. "turn off tomorrow's alarm only": the model
    every = bool(re.search(r"\b(?:all|every|both)\b", t))
    found = _find_time(t)
    if not found:
        m = re.search(r"\b(\d{1,2})(?::(\d\d))?\b", t)       # "the 7 alarm"
        found = (int(m.group(1)), int(m.group(2) or 0), "") if m else None
    said = ""
    if found:
        h, mins, ap = found
        said = f"{h}:{mins:02d}" + (f" {ap}" if ap else "")
    return said, every


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
