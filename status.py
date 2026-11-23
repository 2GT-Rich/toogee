"""'Are you online?' for TooGee: is the Open WebUI server reachable, and is the
language model behind it answering?

parse_status_question() only accepts short, plain forms of the question (it
must match the whole request), so "what's the status of my order?" still goes
to the model. check_server() and check_llm() do the two checks; say() turns
their results into one or two spoken sentences. None of this needs the server
to transcribe the question: when it's down, TooGee's local speech model hears
it.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass

import requests

# ------------------------------------------------------------------ the question
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|hmm|quick question|yo)\s+)*"
    r"(?:(?:(?:can|could|would|will) you (?:please )?|please )?"
    r"(?:(?:tell me|let me know|check|see|i (?:need|want) to know)\s+(?:if|whether)\s+"
    r"|(?:tell me|let me know)\s+))?")
_TRAIL = re.compile(r"(?:\s+(?:please|thanks|thank you|right now|now|still|at the moment|currently|"
                    r"or not|for me|again))+$")

_YOU = r"(?:are you|you are)"
_THING = (r"(?:the |your |my )?(?:server|servers|brain|llm|language model|model|ai|internet|"
          r"connection|network|open ?web ?ui|back ?end)")
_STATE = r"(?:up|online|working|down|offline|reachable|connected|ok|okay|alive|running|available)"

_FORMS = [
    rf"{_YOU} (?:still )?(?:online|offline|connected|up|working|alive|there|okay|ok)",
    rf"{_YOU} (?:connected|talking) to {_THING}",
    rf"(?:can|could) you (?:reach|connect to|talk to|get to|see|hear) {_THING}",
    rf"(?:is|are) {_THING} {_STATE}",
    rf"{_THING} (?:is|are) {_STATE}",
    rf"(?:do you have|have you got|youve got|you have) (?:a |an )?(?:connection|internet|network|signal)",
    rf"hows? (?:is )?{_THING}",
    rf"(?:how|what) (?:is|are) {_THING}(?: doing| status)?",
    r"(?:are )?(?:we|things) (?:online|working|ok|okay|up)",
    r"is everything (?:ok|okay|working|online|up|good|running)",
    r"(?:run )?(?:a )?(?:status(?: check| report)?"
    r"|(?:system|connection|health|network|server)(?: status)? (?:check|report)"
    r"|(?:system|connection|network|server) status)",
    r"(?:run )?(?:a )?diagnostics?",
    rf"(?:whats|what is) (?:your|the) (?:status|connection status|system status)",
    rf"(?:(?:can|could|would|will) you (?:please )?)?check (?:your |the )?"
    rf"(?:connection|status|server|network|internet|systems?)",
]
_RX = re.compile("(?:" + "|".join(_FORMS) + ")")


def _normalize(text: str) -> str:
    t = text.lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\byoure\b", "you are", t)
    t = _LEAD.sub("", t)
    return _TRAIL.sub("", t).strip()


def parse_status_question(text: str) -> bool:
    """True for "are you online?", "can you reach the server?", "is the LLM
    up?", "status check" and similar."""
    t = _normalize(text)
    return bool(t) and len(t.split()) <= 8 and bool(_RX.fullmatch(t))


# ------------------------------------------------------------------ the checks
# A page that answers when the internet works (macOS's own captive-portal check).
INTERNET_PROBE = "http://captive.apple.com/hotspot-detect.html"


@dataclass
class Server:
    ok: bool
    why: str = ""          # "unreachable", "tunnel", "error", "not_open_webui", "local_down"
    detail: str = ""       # for the log
    internet: bool | None = None
    version: str = ""
    sec: float = 0.0


@dataclass
class LLM:
    ok: bool
    why: str = ""          # "key", "no_model", "timeout", "error"
    detail: str = ""
    sec: float = 0.0


def _is_local(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.") or host.endswith(".local")


def _internet() -> bool:
    try:
        return requests.get(INTERNET_PROBE, timeout=4).ok
    except requests.RequestException:
        return False


def _http_detail(r: requests.Response) -> str:
    body = r.text or ""
    if "<html" in body[:500].lower():
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        body = re.sub(r"\s+", " ", m.group(1)).strip() if m else "(an HTML error page)"
    return f"HTTP {r.status_code}: {body[:200]}"


def check_server(url: str, timeout: float = 6.0) -> Server:
    """Open WebUI's /api/version needs no key and answers in ~50 ms, so it tells
    "the server is up" apart from "the model works" (check_llm)."""
    t = time.time()
    try:
        r = requests.get(f"{url}/api/version", timeout=(4, timeout))
    except requests.RequestException as exc:
        sec = time.time() - t
        detail = type(exc).__name__
        if _is_local(url):
            return Server(False, "local_down", detail, sec=sec)
        return Server(False, "unreachable", detail, internet=_internet(), sec=time.time() - t)
    sec = time.time() - t
    if r.status_code == 200:
        try:
            version = str(r.json().get("version", ""))
        except ValueError:
            return Server(False, "not_open_webui", _http_detail(r), internet=True, sec=sec)
        return Server(True, version=version, internet=True, sec=sec)
    # Cloudflare's "tunnel is down" answers: 530 / error 1033, and 502 from the edge.
    body = (r.text or "")[:3000].lower()
    tunnel = r.status_code in (502, 530) and ("cloudflare" in body or "1033" in body or "argo" in body)
    return Server(False, "tunnel" if tunnel else "error", _http_detail(r), internet=True, sec=sec)


def check_llm(url: str, key: str, model: str, timeout: float = 30.0) -> LLM:
    """Ask the model for a few tokens. Any output counts, including a
    reasoning model's thinking: it shows the model is loaded and generating."""
    t = time.time()
    body = {"model": model, "stream": False, "max_tokens": 5,
            "messages": [{"role": "user", "content": "Reply with just the word OK."}]}
    try:
        r = requests.post(f"{url}/api/chat/completions", json=body, timeout=(4, timeout),
                          headers={"Authorization": f"Bearer {key}"})
    except requests.ReadTimeout:
        return LLM(False, "timeout", f"no answer within {timeout:.0f}s", sec=time.time() - t)
    except requests.RequestException as exc:
        return LLM(False, "error", type(exc).__name__, sec=time.time() - t)
    sec = time.time() - t
    if r.status_code in (401, 403):
        return LLM(False, "key", _http_detail(r), sec=sec)
    if r.status_code in (400, 404) and "model not found" in (r.text or "").lower():
        return LLM(False, "no_model", _http_detail(r), sec=sec)
    if r.status_code != 200:
        return LLM(False, "error", _http_detail(r), sec=sec)
    try:
        choice = r.json()["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return LLM(False, "error", f"unexpected reply: {(r.text or '')[:200]}", sec=sec)
    msg = choice.get("message") or {}
    if msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") \
            or choice.get("finish_reason") == "length":
        return LLM(True, sec=sec)
    return LLM(False, "error", f"empty reply: {(r.text or '')[:200]}", sec=sec)


# ------------------------------------------------------------------ the answer
STILL_CAN = "I can still tell you the time, set timers and alarms, and change my volume."


def _seconds(sec: float) -> str:
    if sec < 1:
        return "in under a second"
    n = round(sec)
    return f"in {n} second{'s' if n != 1 else ''}"


def say(server: Server, llm: LLM | None) -> str:
    if server.ok and llm and llm.ok:
        return (f"Yes, I'm online. I'm connected to the server, and the language model "
                f"answered {_seconds(llm.sec)}.")
    if server.ok:
        why = llm.why if llm else "error"
        problem = {
            "key": "it turned down my API key, so I can't use the language model.",
            "no_model": "the language model I'm set to use isn't on it.",
            "timeout": f"the language model didn't answer {_seconds(llm.sec if llm else 30)}. "
                       "It may be busy or still loading.",
        }.get(why, "the language model isn't working right now.")
        return f"I'm connected to the server, but {problem}"
    if server.why == "local_down":
        return f"I can't reach the server. Open WebUI doesn't seem to be running on this Mac. {STILL_CAN}"
    if server.why == "tunnel":
        return ("I'm offline. The internet works, but the server's tunnel is down, "
                f"so I can't reach the language model. {STILL_CAN}")
    if server.why in ("error", "not_open_webui"):
        return ("I'm offline. The server's address answers, but with an error, "
                f"so I can't reach the language model. {STILL_CAN}")
    if server.internet is False:
        return ("I'm offline. I can't reach the internet, so I can't reach the server "
                f"or the language model. {STILL_CAN}")
    return ("I'm offline. The internet works, but I can't reach the server, "
            f"so I can't reach the language model either. {STILL_CAN}")


def log_line(server: Server, llm: LLM | None) -> str:
    s = (f"server ok (Open WebUI {server.version}, {server.sec:.2f}s)" if server.ok
         else f"server {server.why} ({server.detail}; internet {server.internet})")
    if llm is None:
        return s + ", model not checked"
    return s + (f", model ok ({llm.sec:.2f}s)" if llm.ok else f", model {llm.why} ({llm.detail})")
