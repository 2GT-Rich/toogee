#!/usr/bin/env python3
"""TooGee: a voice assistant with a face, on a FreeWili OG running the Wili firmware.

Flow: say "TooGee" (or press a button) -> the board streams its mic to the Mac
-> stop on silence -> Open WebUI Whisper (speech to text) -> Open WebUI chat
model -> Kokoro (text to speech, local) -> streamed back to the board's
speaker while its face lip-syncs. The face shows listening / thinking /
talking, and gets bored and falls asleep on its own when left alone.

Wake word: the board's audio is cut into speech bursts on the Mac, and each
burst is transcribed by a small local Whisper model (faster-whisper base.en,
biased toward spelling the name); a burst that starts with the name wakes
TooGee. Nothing leaves the Mac until it wakes. "TooGee, what time is it?" in
one breath works, as does "TooGee" ... (chime) ... "what time is it?".

Firmware: wiliOGbsp/apps/wili (`python tools/fw.py flash wili`); protocol:
wili_board.py; text to speech: tts/kokoro_server.mjs (kokoro-js).

Buttons (idle):  White = start a new conversation,  any other = talk.

Usage:
  .venv/bin/python wili_assistant.py              # run (wake word + buttons)
  .venv/bin/python wili_assistant.py --no-wake    # push-to-talk only
  .venv/bin/python wili_assistant.py --check      # check Open WebUI, board, TTS
  .venv/bin/python wili_assistant.py --text "hi"  # skip the mic: typed question, spoken reply
  .venv/bin/python wili_assistant.py --wav f.wav  # skip the mic: transcribe a wav file
"""

from __future__ import annotations

import argparse
import faulthandler
import io
import json
import os
import pathlib
import queue
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

import requests
import serial

import wili_board as wb

HERE = pathlib.Path(__file__).resolve().parent
RATE = wb.RATE  # 8 kHz, 16-bit mono both ways

# Whisper tends to "hear" these in silence or noise.
HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching!", "bye.", "."}


def default_prompt(name: str) -> str:
    return (
        f"You are {name}, a small, friendly voice assistant who lives in a handheld gadget with an "
        "animated face. Your replies are spoken aloud through a tiny speaker, so answer in one to "
        "three short sentences of plain conversational text. No markdown, lists, emoji, URLs or "
        "code unless explicitly asked."
    )


# --------------------------------------------------------------------------- config
def load_env(path: pathlib.Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


class Config:
    def __init__(self) -> None:
        load_env(HERE / ".env")
        e = os.environ.get
        self.url = e("OPENWEBUI_URL", "http://localhost:3000").rstrip("/")
        self.key = e("OPENWEBUI_API_KEY", "")
        self.model = e("OPENWEBUI_MODEL", "")
        self.name = e("WILI_NAME", "TooGee")
        self.prompt = e("WILI_SYSTEM_PROMPT", default_prompt(self.name))
        self.max_listen = float(e("WILI_MAX_LISTEN_SEC", "12"))
        self.silence = float(e("WILI_SILENCE_SEC", "1.2"))
        self.history_turns = int(e("WILI_HISTORY_TURNS", "6"))
        # "name" = listen for WILI_NAME with local Whisper; "off" = buttons only
        self.wake = e("WILI_WAKE", "name").lower()
        self.wake_model = e("WILI_WAKE_MODEL", "base.en")
        # text to speech: "kokoro" (default) or "say" (macOS)
        self.tts = e("WILI_TTS", "kokoro").lower()
        # af_sky at 1.1x matches the Kokoro.js voice in your Open WebUI settings
        self.kokoro_voice = e("WILI_KOKORO_VOICE", "af_sky")
        self.kokoro_speed = float(e("WILI_KOKORO_SPEED", "1.1"))
        # let the model search the web (through Open WebUI's search) when it needs to
        self.web_search = e("WILI_WEB_SEARCH", "on").lower() not in ("off", "0", "no", "false")
        # comma-separated names/terms you use that speech recognition mangles
        # (e.g. a brand spelled unusually); used as spelling hints everywhere
        self.vocab = [v.strip() for v in e("WILI_VOCAB", "").split(",") if v.strip()]
        self.say_voice = e("WILI_VOICE", "Samantha")
        # "board" = the board's own speaker (default); "mac" = Mac speakers
        self.speaker = e("WILI_SPEAKER", "board").lower()
        self.volume = int(e("WILI_VOLUME", "9"))
        # LED bar brightness, percent (the full-scale colours are blinding)
        self.led_brightness = float(e("WILI_LED_BRIGHTNESS", "25"))


# --------------------------------------------------------------------------- Open WebUI
class OpenWebUI:
    def __init__(self, cfg: Config) -> None:
        if not cfg.key:
            sys.exit(f"OPENWEBUI_API_KEY is not set. Add it to {HERE / '.env'} (see .env.example).")
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {cfg.key}"

    def _post(self, path: str, read_timeout: float, **kw) -> requests.Response:
        """POST with one retry. An idle keep-alive connection (e.g. through a
        proxy/CDN) can die silently and hang until the read timeout; on a
        timeout or connection error, drop the pooled connections and retry once."""
        for attempt in (1, 2):
            try:
                return self.s.post(f"{self.cfg.url}{path}", timeout=(10, read_timeout), **kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 2:
                    raise RuntimeError(f"Open WebUI didn't answer ({type(exc).__name__})") from exc
                print(f"  (retrying {path} after {type(exc).__name__})")
                self.s.close()
        raise AssertionError("unreachable")

    def models(self) -> list[str]:
        r = self.s.get(f"{self.cfg.url}/api/models", timeout=15)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def transcribe(self, wav_bytes: bytes) -> str:
        # `prompt` is the OpenAI-style spelling hint; it helps Whisper write the
        # assistant's name instead of "P.G." (ignored if the server drops it).
        r = self._post(
            "/api/v1/audio/transcriptions",
            read_timeout=30,
            files={"file": ("wili.wav", wav_bytes, "audio/wav")},
            data={"prompt": ", ".join([self.cfg.name, *self.cfg.vocab]) + "."},
        )
        if r.status_code != 200:
            raise RuntimeError(f"transcription failed: HTTP {r.status_code}: {r.text[:300]}")
        return (r.json().get("text") or "").strip()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One chat completion; returns the assistant message (content and/or
        tool_calls). Reasoning models occasionally return only their reasoning
        and a null answer; ask once more before giving up."""
        body = {"model": self.cfg.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        for attempt in (1, 2):
            r = self._post("/api/chat/completions", read_timeout=60, json=body)
            if r.status_code != 200:
                raise RuntimeError(f"chat failed: HTTP {r.status_code}: {r.text[:300]}")
            msg = r.json()["choices"][0]["message"]
            msg["content"] = (msg.get("content") or "").strip()
            if msg["content"] or msg.get("tool_calls"):
                return msg
            print(f"  (model returned no answer{', retrying' if attempt == 1 else ''})")
        raise RuntimeError("the model returned no answer")

    def web_search(self, query: str) -> tuple[str, list[str]]:
        """Search the web through Open WebUI's own pipeline (its configured
        engine, page loader and vector store). Returns (evidence text for the
        model, list of source URLs).

        Open WebUI only runs its built-in search inside a browser chat session,
        so the `features.web_search` flag does nothing over the API; these two
        retrieval endpoints are the same steps its chat performs.

        Evidence = every result's title and snippet, plus the passages most
        relevant to the query, at most two per page. (Taking the top passages
        alone once gave 4 of 5 from a single irrelevant page.)"""
        r = self._post("/api/v1/retrieval/process/web/search", read_timeout=60,
                       json={"queries": [query], "query": query})
        if r.status_code != 200:
            raise RuntimeError(f"web search failed: HTTP {r.status_code}: {r.text[:200]}")
        found = r.json()
        items = found.get("items") or []
        lines = [f"Search results for: {query}"]
        for i, it in enumerate(items[:8], 1):
            lines.append(f"{i}. {it.get('title', '').strip()} ({it.get('link', '')})\n   {it.get('snippet', '').strip()}")
        sources = [it.get("link", "") for it in items[:8]]

        excerpts: list[str] = []
        if found.get("collection_names"):
            q = self._post("/api/v1/retrieval/query/collection", read_timeout=30,
                           json={"collection_names": found["collection_names"], "query": query, "k": 16})
            if q.status_code == 200:
                res = q.json()
                per_source: dict[str, int] = {}
                for d, m in zip((res.get("documents") or [[]])[0], (res.get("metadatas") or [[]])[0]):
                    src = (m or {}).get("source", "")
                    if per_source.get(src, 0) >= 2:
                        continue
                    per_source[src] = per_source.get(src, 0) + 1
                    excerpts.append(f"[from {src}]\n{d.strip()[:1000]}")
                    if len(excerpts) >= 6:
                        break
        if excerpts:
            lines.append("\nExcerpts from those pages:\n" + "\n\n".join(excerpts))
        if not items and not excerpts:
            lines.append("No results.")
        return "\n".join(lines), sources


# --------------------------------------------------------------------------- audio helpers
def to_wav(samples: list[int], rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def normalize(samples: list[int], target_peak: int = 24000) -> list[int]:
    """Remove DC offset and boost quiet recordings (helps Whisper with a distant mic)."""
    if not samples:
        return samples
    dc = sum(samples) // len(samples)
    s = [x - dc for x in samples]
    peak = max(1, max(abs(x) for x in s))
    gain = min(8.0, target_peak / peak)
    return [max(-32768, min(32767, int(x * gain))) for x in s]


def rms(block: list[int]) -> float:
    return (sum(x * x for x in block) / len(block)) ** 0.5 if block else 0.0


def speakable(text: str) -> str:
    """Strip markdown and symbols that sound bad when read aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)  # reasoning models
    text = re.sub(r"[*_`#>|~]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str, max_len: int = 220) -> list[str]:
    """Split into sentences, merging short ones, so playback can start sooner."""
    out: list[str] = []
    for p in re.split(r"(?<=[.!?])\s+", text):
        p = p.strip()
        if not p:
            continue
        if out and len(out[-1]) + len(p) < 60:
            out[-1] += " " + p
        else:
            out.append(p)
    final: list[str] = []
    for s in out:
        while len(s) > max_len:
            cut = s.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            final.append(s[:cut])
            s = s[cut:].strip()
        if s:
            final.append(s)
    return final


# --------------------------------------------------------------------------- text to speech
class SayTTS:
    """macOS `say`, rendered straight to the board's format."""

    def __init__(self, voice: str) -> None:
        self.voice = voice

    def pcm(self, text: str) -> bytes:
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="wili_")
        os.close(fd)
        try:
            subprocess.run(["say", "-v", self.voice, "-o", name, f"--data-format=LEI16@{RATE}", text],
                           check=True, capture_output=True)
            with wave.open(name) as w:
                return w.readframes(w.getnframes())
        finally:
            pathlib.Path(name).unlink(missing_ok=True)

    def close(self) -> None:
        pass


class KokoroTTS:
    """Kokoro-82M via tts/kokoro_server.mjs (kokoro-js), kept running so the
    model stays loaded. Returns 8 kHz int16 PCM for the board."""

    def __init__(self, voice: str, speed: float) -> None:
        import numpy as np
        from scipy.signal import resample_poly

        self.np, self.resample_poly = np, resample_poly
        self.voice, self.speed = voice, speed
        server = HERE / "tts" / "kokoro_server.mjs"
        if not (HERE / "tts" / "node_modules" / "kokoro-js").exists():
            raise RuntimeError("kokoro-js isn't installed: run `npm install` in tts/")
        self.proc = subprocess.Popen(
            ["node", str(server)], cwd=server.parent, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.lock = threading.Lock()
        self.next_id = 0
        self.ready = threading.Event()
        self.voices: list[str] = []
        threading.Thread(target=self._wait_ready, daemon=True).start()

    def _wait_ready(self) -> None:
        line = self.proc.stdout.readline()
        try:
            self.voices = json.loads(line).get("voices", [])
        except ValueError:
            pass
        self.ready.set()

    def pcm(self, text: str) -> bytes:
        if not self.ready.wait(timeout=60) or self.proc.poll() is not None:
            raise RuntimeError("the Kokoro TTS helper didn't start")
        np = self.np
        with self.lock:
            self.next_id += 1
            req = {"id": self.next_id, "text": text, "voice": self.voice, "speed": self.speed}
            self.proc.stdin.write((json.dumps(req) + "\n").encode())
            self.proc.stdin.flush()
            hdr = json.loads(self.proc.stdout.readline())
            if "error" in hdr:
                raise RuntimeError(f"Kokoro: {hdr['error']}")
            data = self.proc.stdout.read(hdr["bytes"])
        audio = np.frombuffer(data, dtype="<f4")
        rate = int(hdr["rate"])
        if rate % RATE == 0:
            audio = self.resample_poly(audio, 1, rate // RATE)  # 24 kHz -> 8 kHz, anti-aliased
        else:
            audio = self.resample_poly(audio, RATE, rate)
        return (np.clip(audio, -1.0, 1.0) * 32000).astype("<i2").tobytes()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def make_tts(cfg: Config):
    if cfg.tts == "kokoro":
        try:
            return KokoroTTS(cfg.kokoro_voice, cfg.kokoro_speed)
        except Exception as exc:
            print(f"  (Kokoro unavailable, using macOS say: {exc})")
    return SayTTS(cfg.say_voice)


# --------------------------------------------------------------------------- wake by name
class WakeName:
    """Hears the assistant's name in a speech burst, with a local Whisper model.

    Whisper doesn't know "TooGee", so it's nudged with an initial prompt and the
    match accepts the spellings it tends to produce instead ("Too Gee", "2G",
    "Toogie", ...). Every burst's local transcript is logged, so new misspellings
    are easy to spot and add."""

    def __init__(self, name: str, model: str) -> None:
        import numpy as np
        from faster_whisper import WhisperModel
        from scipy.signal import resample_poly

        self.np, self.resample_poly = np, resample_poly
        self.name = name
        self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.pattern = self._pattern(name)

    @staticmethod
    def _pattern(name: str) -> re.Pattern:
        if name.lower() == "toogee":
            first = r"(?:too|two|to|tu|2|doo|du|tou)"
            second = r"(?:gee|g|ghee|jee|ji|gi|gie|gy|ge|jie|ghi|key)"
            one = r"(?:toogee|toogie|toogy|tugee|tugi|tuji|tooji|toojee|tougie|tougee|2g|twogee|doogie|tooky|toojie)"
            core = rf"(?:{one}|{first}[\s-]*{second})"
        else:
            core = r"[\s-]*".join(map(re.escape, name.lower().split()))
        # optional "hey"/"hi"/"ok" first; the name must open the utterance
        return re.compile(rf"^\W*(?:(?:hey|hi|hello|ok|okay|yo|oh)\W+)?{core}\b\W*", re.I)

    def transcribe(self, samples: list[int]) -> str:
        return self._transcribe(samples)[0]

    def find(self, samples: list[int]) -> tuple[str, str | None, float | None]:
        """Transcribe a burst; return (text, rest-after-name or None, time in
        seconds where the name ends, if known). The time lets the caller send
        just the request audio to a stronger recogniser."""
        # Fast pass first: most bursts are just room talk, and word timings
        # are only worth computing once the name has matched with more after it.
        text, _ = self._transcribe(samples)
        m = self.pattern.match(text.lower())
        if not m:
            return text, None, None
        if len(text[m.end():].split()) < 2:
            return text, text[m.end():].strip(), None
        text, words = self._transcribe(samples, word_times=True)
        m = self.pattern.match(text.lower())
        if not m:  # the second decode disagreed; trust the first
            return text, None, None
        # The match ends after the name's trailing punctuation/space; the word
        # whose text reaches past the name's last letter is the name itself.
        last_letter = len(text[: m.end()].rstrip(" ,.!?;:-"))
        pos, name_end = 0, None
        for word, end in words:
            pos += len(word)
            if pos >= last_letter:
                name_end = end
                break
        return text, text[m.end():].strip(), name_end

    def _transcribe(self, samples: list[int], word_times: bool = False):
        np = self.np
        a = np.asarray(samples, dtype=np.float32) / 32768.0
        a16 = self.resample_poly(a, 2, 1).astype(np.float32)
        # Tuned on real recordings of the name (2026-11-20): with no hint
        # base.en heard "TooGee" 3/8 times ("Told you", "Thank you", "2G");
        # a short prompt 6/8; this longer prompt plus the hotword boost 7/8,
        # with no false wakes on ordinary sentences, at ~0.25 s per burst.
        # temperature 0 only: Whisper's default retries at higher temperatures
        # when a burst looks like gibberish (mumbles, TV), up to 6 decodes --
        # one 7 s burst took 10 s. max_new_tokens caps runaway "Mm hmm" loops;
        # the name plus a request fits easily.
        n = self.name
        segs, _ = self.model.transcribe(
            a16, language="en", beam_size=1, temperature=0.0, max_new_tokens=64,
            initial_prompt=f"Hey {n}! {n}, what time is it? Thanks, {n}.", hotwords=n,
            vad_filter=False, without_timestamps=not word_times, word_timestamps=word_times,
            condition_on_previous_text=False,
        )
        segs = list(segs)
        text = "".join(s.text for s in segs).strip()
        words = [(w.word, w.end) for s in segs for w in (s.words or [])]
        if words:  # keep char offsets aligned with `text` (drop the leading space)
            words[0] = (words[0][0].lstrip(), words[0][1])
        return text, words

    def match(self, text: str) -> str | None:
        """None if the text doesn't open with the name; else what follows it."""
        m = self.pattern.match(text.lower())
        return None if not m else text[m.end():].strip()

    def strip(self, text: str) -> str:
        rest = self.match(text)
        return text if rest is None else rest


# --------------------------------------------------------------------------- the board
class BoardLost(Exception):
    """The board's serial port went away (unplugged, reset, USB hiccup)."""


class Board:
    """The Wili board: mic stream, speaker, face, buttons (see wili_board.py)."""

    def __init__(self, cfg: Config, tts) -> None:
        self.cfg, self.tts = cfg, tts
        self.wb = wb.WiliBoard()
        print(f"Board: {self.wb.hello} on {self.wb.port}")
        self.wb.volume(cfg.volume)
        self.wb.led_brightness(cfg.led_brightness)
        self.audio: list[int] = []
        self.mood: int | None = None

    def close(self) -> None:
        self.wb.close()

    def face(self, mood: int, force: bool = False) -> None:
        if force or mood != self.mood:
            self.wb.face(mood)
            self.mood = mood

    def chime(self, which: int) -> None:
        self.wb.chime(which)

    # -- input ---------------------------------------------------------------
    def start_stream(self) -> None:
        self.audio = []
        self.wb.mic_stream(True)

    def stop_stream(self) -> None:
        self.wb.mic_stream(False)

    def pump(self, timeout: float = 0.05) -> None:
        """Move any mic audio the board has sent into self.audio."""
        block = self.wb.read_mic(timeout=timeout)
        while block:
            self.audio.extend(block)
            block = self.wb.read_mic(timeout=0)

    def presses(self) -> list[str]:
        out = []
        while not self.wb.events.empty():
            kind, what = self.wb.events.get_nowait()
            if kind == "press":
                out.append(str(what))
            elif kind == "error":
                raise BoardLost(str(what))
        return out

    def wait_for_button(self) -> str:
        while True:
            p = self.presses()
            if p:
                return p[0]
            time.sleep(0.03)

    def catch_up(self) -> None:
        """Pull everything the board has already sent, so len(self.audio) is
        'now' (mic audio queues up while we transcribe or think)."""
        block = self.wb.read_mic(timeout=0)
        while block:
            self.audio.extend(block)
            block = self.wb.read_mic(timeout=0)

    def chime_and_capture(self, noise: float | None = None) -> list[int]:
        """Chime, then record the request, starting once the chime has died away.

        The mic hears the chime ~50 ms after it is sent, loud for ~250 ms.
        Marking the send point only works if the queued audio from *before*
        it has been pulled in first -- otherwise the mark lands in old silence,
        the capture swallows the chime as "speech", and Whisper turns it into
        "Thank you." (seen 2026-11-20)."""
        self.catch_up()
        sent_at = len(self.audio)
        self.chime(wb.CHIME_LISTEN)
        return self.capture(noise=noise, chime_at=sent_at)

    def _after_chime(self, sent_at: int, threshold: float) -> int:
        """Index where the chime is over: wait to see it (up to 0.6 s), then
        until a 50 ms block is back under the speech threshold (up to 1.2 s)."""
        half = RATE // 20
        pos, seen = sent_at, False
        limit_seen, limit = sent_at + int(0.6 * RATE), sent_at + int(1.2 * RATE)
        while pos < limit:
            while len(self.audio) < pos + half:
                self.pump()
            level = rms(self.audio[pos: pos + half])
            if not seen:
                seen = level > max(3000.0, threshold * 4)
                if not seen and pos >= limit_seen:
                    break  # never heard it (muted?); just start
            elif level <= threshold:
                break
            pos += half
        return max(pos, sent_at + int(0.25 * RATE))

    def capture(self, noise: float | None = None, wait_speech_sec: float = 5.0,
                chime_at: int | None = None) -> list[int]:
        """Record from the running stream until the speaker goes quiet.

        Starts at the current end of self.audio, or -- if `chime_at` marks
        where a chime was sent -- once that chime has died away. With
        noise=None, the first 300 ms is taken as room noise. Returns [] if
        nobody speaks.
        """
        blk = RATE // 10  # 100 ms blocks
        threshold = max(400.0, noise * 3) if noise else 400.0
        begin = checked = len(self.audio)
        if chime_at is not None:
            begin = checked = self._after_chime(chime_at, threshold)
        start = time.time()
        spoke_at = last_loud = None
        loud_blocks = 0
        while True:
            self.pump()
            now = time.time()
            while len(self.audio) - checked >= blk:
                if checked < begin:  # still inside the skipped chime
                    checked = min(begin, len(self.audio))
                    continue
                level = rms(self.audio[checked: checked + blk])
                checked += blk
                if noise is None and checked - begin >= 3 * blk:
                    noise = rms(self.audio[begin:checked])
                    threshold = max(400.0, noise * 3)
                if noise is not None and level > threshold:
                    last_loud = now
                    spoke_at = spoke_at or now
                    loud_blocks += 1
            if spoke_at is None and now - start > wait_speech_sec:
                return []
            if last_loud and now - last_loud > self.cfg.silence:
                break
            if now - start > self.cfg.max_listen:
                break
        # Under 0.3 s of actual sound is a cough or a click, not a request --
        # sending it to Whisper just yields "Thanks for watching!".
        return self.audio[begin:] if loud_blocks >= 3 else []

    def record(self) -> list[int]:
        """Push-to-talk: chime, listen until quiet, chime."""
        self.start_stream()
        try:
            return self.chime_and_capture()
        finally:
            self.stop_stream()

    # -- output --------------------------------------------------------------
    def say(self, text: str) -> None:
        """Speak sentence by sentence. A producer thread synthesises ahead while
        earlier sentences play, so after the first one there are no gaps."""
        chunks = sentences(speakable(text)) or [text]
        q: queue.Queue = queue.Queue(maxsize=3)

        def produce() -> None:
            for c in chunks:
                try:
                    q.put(self.tts.pcm(c))
                except Exception as exc:
                    q.put(exc)
                    return
            q.put(None)

        threading.Thread(target=produce, daemon=True).start()
        pending = q.get()
        while pending is not None:
            if isinstance(pending, Exception):
                raise pending
            nxt = q.get()
            self.wb.play(pending, final=nxt is None or isinstance(nxt, Exception))
            pending = nxt


# --------------------------------------------------------------------------- assistant
class Assistant:
    def __init__(self, cfg: Config, board: Board | None, owui: OpenWebUI, tts,
                 wake: WakeName | None = None) -> None:
        self.cfg, self.board, self.owui, self.tts, self.wake = cfg, board, owui, tts, wake
        self.history: list[dict] = []

    def face(self, mood: int, force: bool = False) -> None:
        if self.board:
            self.board.face(mood, force)

    def chime(self, which: int) -> None:
        if self.board:
            self.board.chime(which)

    TOOLS = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current or factual information you don't know: news, "
                           "weather, sports, prices, opening hours, recent events, people, places.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "a good web search query"}},
                "required": ["query"],
            },
        },
    }]
    MAX_TOOL_ROUNDS = 3
    FILLERS = ["Let me look that up.", "One sec, I'll check.", "Let me find out.", "Good question, let me look."]

    def ask(self, text: str) -> str:
        now = time.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
        system = f"{self.cfg.prompt}\nThe current local date and time is {now}."
        tools = self.TOOLS if self.cfg.web_search else None
        if tools:
            system += (
                " You can call web_search for anything current or that you aren't sure of. "
                "When you have searched, your answer must come from the search results only: "
                "state only facts the results actually contain, and never fill gaps from memory "
                "or guesswork. If the results don't clearly answer the question, or seem to be "
                "about something else, say plainly that you couldn't find it (and what you did "
                "find, if useful) rather than making up an answer. Answer in a sentence or two, "
                "the way you'd say it out loud; don't read out URLs."
            )
        if self.cfg.vocab:
            system += (f" Names the user may mention, spelled correctly: {', '.join(self.cfg.vocab)}. "
                       "Speech recognition may mangle them (e.g. into ordinary words); search for "
                       "the correct spelling.")
        msgs = [{"role": "system", "content": system}]
        msgs += self.history[-2 * self.cfg.history_turns:]
        msgs.append({"role": "user", "content": text})

        said_filler = False
        for _ in range(self.MAX_TOOL_ROUNDS + 1):
            msg = self.owui.chat(msgs, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            results = self._run_tools(calls, speak_filler=not said_filler)
            said_filler = True
            for call, result in zip(calls, results):
                msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        else:
            # Still asking for tools after the last round: make it answer.
            msg = self.owui.chat(msgs + [{"role": "user", "content": "Please answer now from what you found."}])
        reply = msg.get("content") or "Sorry, I couldn't find an answer to that."
        # Only the question and the final answer go into the history; the
        # search results would crowd out the conversation.
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        return reply

    def _run_tools(self, calls: list[dict], speak_filler: bool) -> list[str]:
        """Run the model's tool calls. Searches run in the background while
        TooGee says "let me look that up", since a search takes ~5-10 s."""
        results: list[str] = ["Unknown tool."] * len(calls)

        def run(i: int, call: dict) -> None:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if fn.get("name") == "web_search" and args.get("query"):
                t = time.time()
                try:
                    evidence, sources = self.owui.web_search(str(args["query"]))
                    sites = sorted({re.sub(r"^https?://(www\.)?", "", s).split("/")[0] for s in sources if s})
                    print(f"  (searched {args['query']!r} in {time.time() - t:.1f}s; sources: {', '.join(sites) or 'none'})")
                    results[i] = (
                        f"{evidence}\n\n"
                        "Answer only from the results above. If they don't clearly answer the "
                        "user's question, say you couldn't find it -- do not guess."
                    )
                except Exception as exc:
                    results[i] = f"The search failed ({exc}). Tell the user you couldn't search right now."
                    print(f"  (search failed: {exc})")

        threads = [threading.Thread(target=run, args=(i, c), daemon=True) for i, c in enumerate(calls)]
        for th in threads:
            th.start()
        if speak_filler:
            self.speak(self.FILLERS[int(time.time()) % len(self.FILLERS)])
        self.face(wb.THINK)
        for th in threads:
            th.join(timeout=90)
        return results

    def speak(self, text: str) -> None:
        self.face(wb.TALK)
        if self.board and self.cfg.speaker == "board":
            self.board.say(text)
        else:
            pcm = self.tts.pcm(text)
            subprocess.run(["afplay", self._tmp_wav(pcm)], check=False)

    @staticmethod
    def _tmp_wav(pcm: bytes) -> str:
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="wili_")
        os.close(fd)
        with wave.open(name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
        return name

    def respond(self, heard: str) -> None:
        print(f"you:  {heard}")
        self.face(wb.THINK)
        t = time.time()
        spoken = speakable(self.ask(heard))
        print(f"{self.cfg.name.lower()}: {spoken}   ({time.time() - t:.1f}s)")
        self.speak(spoken)
        self.face(wb.IDLE)

    def turn(self, samples: list[int] | None = None, heard: str | None = None,
             fallback: str | None = None) -> None:
        """One exchange. Records push-to-talk style unless samples (or an
        already-transcribed request) are given. `fallback` is a local
        transcript to use if Open WebUI's comes back as a silence hallucination."""
        b = self.board
        assert b
        if heard is None:
            if samples is None:
                self.face(wb.LISTEN)
                samples = b.record()
            if not samples:
                print("  (didn't hear anything)")
                self.face(wb.CONFUSED)
                return
            self.chime(wb.CHIME_DONE)
            self.face(wb.THINK)
            t = time.time()
            heard = self.owui.transcribe(to_wav(normalize(samples)))
            print(f"  (heard {len(samples) / RATE:.1f}s of audio, transcribed in {time.time() - t:.1f}s)")
            if fallback and heard.lower().strip() in HALLUCINATIONS:
                print(f"  (Open WebUI heard {heard!r}; using the local transcript)")
                heard = fallback
        else:
            self.chime(wb.CHIME_DONE)
        if self.wake:
            heard = self.wake.strip(heard)
        if heard.lower().strip() in HALLUCINATIONS:
            print(f"  (ignored transcript {heard!r})")
            self.speak("Sorry, I didn't catch that.")
            self.face(wb.CONFUSED)
            return
        self.respond(heard)

    def new_conversation(self) -> None:
        self.history.clear()
        print("  (new conversation)")
        self.speak("Okay, starting fresh.")
        self.face(wb.HAPPY)

    def guarded(self, fn, *args) -> None:  # keep running on network/API hiccups
        try:
            fn(*args)
        except Exception as exc:
            print(f"error: {exc}")
            self.chime(wb.CHIME_ERROR)
            self.face(wb.SAD)
            time.sleep(2)

    def run(self) -> None:
        if self.wake:
            return self.run_wake()
        b = self.board
        assert b
        print("Ready. Press a button on the board and talk (White = new conversation). Ctrl-C quits.")
        self.face(wb.IDLE)
        while True:
            button = b.wait_for_button()
            self.guarded(self.new_conversation if button == "gray" else self.turn)

    # -- wake word -------------------------------------------------------------
    BURST_ON = 3.5        # x noise floor = speech
    BURST_END_SEC = 0.7   # this much quiet ends a burst
    BURST_MAX_SEC = 7.0
    PREROLL_SEC = 0.3

    def run_wake(self) -> None:
        """Cut the mic stream into speech bursts; a burst that opens with the
        name wakes the assistant. Buttons work too."""
        b, wake = self.board, self.wake
        assert b and wake
        print(f'Ready. Say "{wake.name}" or press a button (White = new conversation). Ctrl-C quits.')
        self.face(wb.IDLE)
        blk = RATE // 10
        while True:
            b.start_stream()
            b.presses()  # drop presses from before we were listening
            noise = 100.0
            checked = 0
            burst_start: int | None = None
            quiet_blocks = 0
            trigger: str | None = None
            command: list[int] = []
            request: str | None = None   # "TooGee, <request>" in one breath
            fallback: str | None = None  # local transcript of that request
            try:
                while trigger is None:
                    b.pump()
                    for press in b.presses():
                        trigger = "white" if press == "gray" else "button"
                        print(f"  ({press} button)")
                        break
                    while trigger is None and len(b.audio) - checked >= blk:
                        level = rms(b.audio[checked: checked + blk])
                        loud = level > max(350.0, noise * self.BURST_ON)
                        if burst_start is None:
                            if loud:
                                burst_start = max(0, checked - int(self.PREROLL_SEC * RATE))
                                quiet_blocks = 0
                            elif level < noise * 2:  # track the room's noise floor
                                noise = 0.95 * noise + 0.05 * level
                        else:
                            quiet_blocks = 0 if loud else quiet_blocks + 1
                        checked += blk
                        if burst_start is None:
                            continue
                        dur = (checked - burst_start) / RATE
                        if quiet_blocks * 0.1 >= self.BURST_END_SEC or dur >= self.BURST_MAX_SEC:
                            burst = b.audio[burst_start:checked]
                            burst_start = None
                            if dur - quiet_blocks * 0.1 < 0.25:
                                continue  # a click, not speech
                            t = time.time()
                            text, rest, name_end = wake.find(burst)
                            if text:  # wordless noise bursts would just spam the log
                                print(f"  (heard locally in {time.time() - t:.2f}s: {text!r}"
                                      f"{' -> WAKE' if rest is not None else ''})")
                            if rest is None:
                                continue
                            if len(rest.split()) >= 2:
                                # "TooGee, <request>" in one breath. Open
                                # WebUI's Whisper hears "TooGee" as "Thank
                                # you" but is better at the request, so send
                                # it only the audio after the name; fall back
                                # to the local transcript.
                                trigger, request = "wake+command", rest
                                cut = int(((name_end or 0) + 0.05) * RATE)
                                if name_end and len(burst) - cut > int(0.5 * RATE):
                                    command, request, fallback = burst[cut:], None, rest
                            else:
                                trigger = "wake"
                            break
                    if checked > RATE * 30 and burst_start is None:
                        del b.audio[:checked]  # don't let the idle buffer grow forever
                        checked = 0
                if trigger in ("wake", "button"):
                    # perk up, chime, and listen for the request
                    self.face(wb.WAKE, force=True)
                    command = b.chime_and_capture(noise=noise)
                elif trigger == "wake+command":
                    self.face(wb.WAKE, force=True)
            finally:
                # Stop listening while we think and talk, so it can't wake itself up.
                b.stop_stream()
            if trigger == "white":
                self.guarded(self.new_conversation)
            elif request is not None:
                self.guarded(self.turn, None, request)
            else:
                self.guarded(self.turn, command, None, fallback)


def check(cfg: Config, owui: OpenWebUI) -> None:
    print(f"Open WebUI: {cfg.url}")
    models = owui.models()
    print(f"Models ({len(models)}):")
    for m in models:
        print(f"  {'*' if m == cfg.model else ' '} {m}")
    if not cfg.model:
        print("OPENWEBUI_MODEL is not set; pick one of the above and put it in .env")
    elif cfg.model not in models:
        print(f"OPENWEBUI_MODEL={cfg.model!r} is not in the list above")
    try:
        owui.transcribe(to_wav([0] * RATE))
        print("Speech to text: OK")
    except Exception as exc:
        print(f"Speech to text: FAILED ({exc})")
    if cfg.web_search:
        try:
            t = time.time()
            res, sources = owui.web_search("weather forecast today")
            print(f"Web search: OK, {len(sources)} results, {res.count('[from ')} excerpts in {time.time() - t:.1f}s")
        except Exception as exc:
            print(f"Web search: FAILED ({exc})")
    port = wb.find_port()
    print(f"Wili board: {port or 'not found (is the Wili firmware flashed?)'}")
    tts = make_tts(cfg)
    try:
        t = time.time()
        n = len(tts.pcm("Testing.")) // 2
        print(f"Text to speech ({type(tts).__name__}): OK, {n / RATE:.1f}s of audio in {time.time() - t:.1f}s")
    except Exception as exc:
        print(f"Text to speech: FAILED ({exc})")
    finally:
        tts.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="check Open WebUI, the board and TTS")
    ap.add_argument("--text", help="send this text instead of using the mic")
    ap.add_argument("--wav", help="transcribe this wav file instead of using the mic")
    ap.add_argument("--mac-speaker", action="store_true", help="speak on the Mac, not the board")
    ap.add_argument("--no-wake", action="store_true", help="push-to-talk only, no wake word")
    args = ap.parse_args()

    # SIGTERM (kill, launchd stop) -> normal exit, so the board is told to stop
    # streaming and return to idle. `kill -USR1 <pid>` dumps every thread's stack.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    cfg = Config()
    if args.mac_speaker:
        cfg.speaker = "mac"
    owui = OpenWebUI(cfg)
    if args.check:
        return check(cfg, owui)
    if not cfg.model:
        sys.exit("OPENWEBUI_MODEL is not set. Run with --check to list models.")
    if args.wav:
        print(owui.transcribe(pathlib.Path(args.wav).read_bytes()))
        return

    tts = make_tts(cfg)
    wake = None
    if not (args.text or args.no_wake or cfg.wake in ("off", "none", "")):
        print(f"Loading the local wake-word model ({cfg.wake_model}) ...")
        wake = WakeName(cfg.name, cfg.wake_model)
    board = None
    try:
        if args.text:
            if cfg.speaker != "mac":
                board = Board(cfg, tts)
            Assistant(cfg, board, owui, tts, wake).respond(args.text)
            return
        a: Assistant | None = None
        waiting = False
        while True:
            try:
                board = Board(cfg, tts)
                waiting = False
                if a is None:
                    a = Assistant(cfg, board, owui, tts, wake)
                else:
                    a.board = board  # same conversation, reconnected board
                a.run()
            except (serial.SerialException, OSError, BoardLost) as exc:
                # The board dropped off USB (cable bump, hub reset, reboot):
                # wait for it to come back instead of exiting.
                print(f"  (lost the board: {exc}; waiting for it to come back ...)")
            except RuntimeError as exc:
                if "No Wili board found" not in str(exc):
                    raise
                if not waiting:
                    print("  (no Wili board on USB; waiting for it ...)")
            waiting = True
            if board:
                try:
                    board.close()
                except Exception:
                    pass
                board = None
            time.sleep(2)
    except KeyboardInterrupt:
        print()
    except RuntimeError as exc:
        sys.exit(str(exc))
    finally:
        if board:
            try:
                board.close()
            except Exception:
                pass
        tts.close()


if __name__ == "__main__":
    main()
