"""'How's your battery?' for TooGee, from the board's BQ25896 charger.

The charger reports the battery voltage, whether it's charging, and whether
USB power is present; it has no fuel gauge, so the charge level is estimated
from the voltage (a typical single-cell LiPo curve) and always said as
"about". While charging, the voltage reads high, so a charging estimate is
taken a little lower.
"""

from __future__ import annotations

import re

_LEAD = re.compile(r"^(?:(?:hey|hi|ok|okay|so|um|uh|hmm|quick question|yo)\s+)*"
                   r"(?:(?:can|could|would|will) you (?:please )?(?:tell me|check)\s+(?:if |whether )?"
                   r"|(?:please )?(?:tell me|check)\s+(?:if |whether )?)?")
_TRAIL = re.compile(r"(?:\s+(?:please|thanks|thank you|right now|now|still|at the moment|currently|"
                    r"or not|for me|again|left|remaining))+$")
_FORMS = [
    r"(?:hows|how is|whats|what is|check)(?: your| the)? battery(?: level| status| life| charge| doing)?",
    r"(?:your |the )?battery(?: level| status| life| check| charge)?",
    r"how (?:much|full) is (?:your |the )?battery",
    r"how much (?:battery|charge) (?:do you have|have you got|is there|is left)",
    r"how (?:much )?charged are you",
    r"(?:are you|you are) (?:charging|charged|plugged in|on battery|running on battery|fully charged)",
    r"is (?:your |the )?battery (?:full|low|charged|charging|ok|okay|good|dead|dying)",
    r"(?:do|does) (?:you|it) need (?:to be )?(?:charged|charging|a charge|to charge)",
    r"what(?:s| is) your (?:charge|battery) (?:level|percentage|percent)",
]
_RX = re.compile("(?:" + "|".join(_FORMS) + ")")


def _normalize(text: str) -> str:
    t = text.lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\byoure\b", "you are", t)
    return _TRAIL.sub("", _LEAD.sub("", t)).strip()


def parse_battery_question(text: str) -> bool:
    t = _normalize(text)
    return bool(t) and len(t.split()) <= 8 and bool(_RX.fullmatch(t))


# Resting voltage -> charge, for a typical LiPo cell.
_CURVE = [(4200, 100), (4150, 95), (4110, 90), (4080, 85), (4020, 80), (3980, 75), (3950, 70),
          (3910, 65), (3870, 60), (3850, 55), (3840, 50), (3820, 45), (3800, 40), (3790, 35),
          (3770, 30), (3750, 25), (3730, 20), (3710, 15), (3690, 10), (3610, 5), (3300, 0)]

CHARGING_SAG_MV = 100   # a charging cell reads about this much above its resting voltage


def percent(vbat_mv: int, charging: bool = False) -> int:
    v = vbat_mv - (CHARGING_SAG_MV if charging else 0)
    if v >= _CURVE[0][0]:
        return 100
    for (v_hi, p_hi), (v_lo, p_lo) in zip(_CURVE, _CURVE[1:]):
        if v >= v_lo:
            return round(p_lo + (p_hi - p_lo) * (v - v_lo) / (v_hi - v_lo))
    return 0


def _about(p: int) -> str:
    return f"about {5 * round(p / 5)} percent"


def describe(power) -> str:
    """One or two sentences from a wili_board.PowerStatus (or None)."""
    if power is None:
        return "I can't read my battery. My board's firmware is too old to report it."
    if not power.ok:
        return "I can't read my battery right now. My charger isn't answering."
    charging = power.chg_stat in (1, 2)
    full = power.chg_stat == 3
    p = percent(power.vbat_mv, charging)
    if full:
        level = "My battery is full"
    elif charging:
        level = f"I'm charging, and my battery is at {_about(p)}"
    else:
        level = f"My battery is at {_about(p)}"
    if power.usb_power:
        plug = ", and I'm plugged in." if not charging else "."
        if not (full or charging) and p < 95:
            plug = ", and I'm plugged in but not charging."
    else:
        plug = ", and I'm running on it, not on USB power."
    out = level + plug
    problem = {1: "There's a problem with my power input.",
               2: "My charger got too hot and shut off for now.",
               3: "Charging took too long, so my charger stopped."}.get(power.fault)
    if problem:
        out += " " + problem
    if power.ts_rank in (5, 6):
        out += f" My battery is too {'cold' if power.ts_rank == 5 else 'hot'} to charge."
    elif power.thermal_reg:
        out += " I'm charging slowly because I'm warm."
    return out


def log_line(power) -> str:
    if power is None:
        return "no power report (firmware before 005)"
    if not power.ok:
        return "charger not answering"
    stat = {0: "not charging", 1: "pre-charge", 2: "fast charge", 3: "done"}.get(power.chg_stat, str(power.chg_stat))
    return (f"{stat}, battery {power.vbat_mv} mV (~{percent(power.vbat_mv, power.chg_stat in (1, 2))}%), "
            f"USB {'yes' if power.usb_power else 'no'} {power.vbus_mv} mV, charge {power.ichg_ma} mA, "
            f"fault {power.fault}, thermistor rank {power.ts_rank}")
