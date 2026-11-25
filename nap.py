"""Telling TooGee to sleep: "go to sleep for 30 minutes", "take a nap until
3 pm", "stop listening for an hour", or just "go to sleep" (until woken).

While it sleeps the mic is off, so a noisy room can't wake it by mishearing
its name; alarms and timers still ring. A triple tap or any button wakes it.
"""

from __future__ import annotations

import re
import time

import alarms

MAX_SEC = 24 * 3600

_LEAD = re.compile(
    r"^(?:(?:hey|ok|okay|so|alright|all right|now|please|toogee|too gee|then|and|and then|after that|also|"
    r"can you|could you|would you|will you|why dont you|go ahead and|you can|"
    r"i want you to|i need you to|id like you to|time to|its time to|you should|just)\s+)*")
_VERB = re.compile(
    r"^(?:go (?:back )?to sleep|go to bed|go sleep|sleep|take a nap|have a nap|nap|"
    r"stop listening|quit listening|dont listen|stop listening to me|mute your (?:mic|microphone|ears))\b")
_FILLER = re.compile(r"\b(?:please|now|for now|for me|ok|okay|thanks|thank you|toogee|too gee|then|a while|"
                     r"a bit|a little|while|until i wake you(?: up)?|until i say so|until i tell you|"
                     r"till i wake you(?: up)?|til i wake you(?: up)?)\b")


def parse_sleep_command(text: str, now: float | None = None) -> tuple[float, bool] | None:
    """(how long to sleep in seconds, 0 = until woken; whether it was said as
    a clock time, "until 3 pm"). None if `text` isn't (simply) telling it to
    sleep: that is left to the model."""
    now = time.time() if now is None else now
    t = alarms._clean(text)
    t = _LEAD.sub("", t)
    m = _VERB.match(t)
    if not m or len(t.split()) > 14:
        return None
    rest = t[m.end():].strip()

    # "for 30 minutes", "for an hour and a half", "for 1 hour 20 minutes"
    total, found = 0.0, False
    for d in re.finditer(rf"\b{alarms._NUM} ({'|'.join(alarms._UNIT)})s?\b", rest):
        n = alarms._num(d.group(1))
        if n is None:
            continue
        total += n * alarms._UNIT[d.group(2)]
        found = True
    if found:
        if re.search(r"\band a half\b", rest):
            unit = re.findall(rf"\b({'|'.join(alarms._UNIT)})s?\b", rest)
            total += 0.5 * alarms._UNIT[unit[-1]]
        return (min(total, MAX_SEC), False) if total > 0 else None

    # "until 3 pm", "till 7:30", "until noon", "until morning"
    u = re.search(r"\b(?:until|till|til|to|up to)\b (.+)$", rest)
    if u and not re.match(r"(?:i|you)\b", u.group(1)):
        target = u.group(1)
        if re.fullmatch(r"(?:the )?morning(?: please)?", target):
            target = "7 am"
        found_t = alarms._find_time("at " + target)
        if not found_t:
            return None
        h, mins, ap = found_t
        if h > 23 or mins > 59 or (ap and not 1 <= h <= 12):
            return None
        if ap:
            h = h % 12 + (12 if ap == "pm" else 0)
        else:
            # "until 3": the next 3 o'clock, morning or afternoon
            lt = time.localtime(now)
            if h <= 12:
                cands = [h % 12, h % 12 + 12]
                later = [c for c in cands if (c, mins) > (lt.tm_hour, lt.tm_min)]
                h = later[0] if later else cands[0]
        due = alarms.next_at(f"{h:02d}:{mins:02d}", after=now)
        return min(due - now, MAX_SEC), True

    # nothing but filler left: until woken
    if not _FILLER.sub("", rest).strip():
        return 0.0, False
    return None


def countdown(seconds: float) -> str:
    """'29:41', '1:05:00'."""
    s = max(0, int(seconds + 0.999))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def say_for(seconds: float, until: bool = False, now: float | None = None) -> str:
    """'for 30 minutes', 'until 3 PM', 'until you wake me', for TooGee's reply."""
    now = time.time() if now is None else now
    if seconds <= 0:
        return "until you wake me"
    if until:
        return "until " + alarms.say_clock(now + seconds, now)
    return "for " + alarms.say_duration(seconds)
