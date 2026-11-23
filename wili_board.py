"""Host side of the Wili firmware protocol (wiliOGbsp/apps/wili/display/wili_proto.h).

The FreeWili OG's display CPU, running the Wili firmware, shows up as a USB
serial port (VID:PID 093C:2055, product "FWOG display wili ..."). Frames:

    0xF7 0x1A | type u8 | len u16 LE | payload | crc8 (poly 0x07 over type+len+payload)

Audio is 8 kHz int16 mono both ways. A reader thread parses incoming frames;
mic audio, button presses and face changes are delivered through queues and
counters, so callers never touch the serial port directly.
"""

from __future__ import annotations

import collections
import queue
import struct
import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

RATE = 8000
VID, PID_DISPLAY = 0x093C, 0x2055

MAGIC = b"\xf7\x1a"
EV_HELLO, EV_MIC, EV_BUTTON, EV_SPK, EV_FACE = 0x01, 0x02, 0x03, 0x04, 0x05
EV_ACCEL, EV_POWER = 0x06, 0x07   # firmware 005+
CMD_PING, CMD_MIC, CMD_AUDIO, CMD_AUDIO_END, CMD_FLUSH = 0x10, 0x11, 0x12, 0x13, 0x14
CMD_FACE, CMD_VOLUME, CMD_LEDS, CMD_CHIME, CMD_LED_LEVEL = 0x15, 0x16, 0x17, 0x18, 0x19
CMD_ROTATE = 0x1A   # firmware 006+
CMD_CAPTION = 0x1B  # firmware 009+
CAPTION_MAX = 24
MAX_PAYLOAD = 1024

# Built-in earcons (firmware 002+)
CHIME_LISTEN, CHIME_DONE, CHIME_ERROR = 1, 2, 3
CHIME_SEC = {CHIME_LISTEN: 0.215, CHIME_DONE: 0.19, CHIME_ERROR: 0.35}

# Moods (face.h wili_mood_t)
IDLE, LISTEN, THINK, TALK, HAPPY, CONFUSED, SAD, SLEEP, BORED, WAKE = range(10)
MOOD_NAMES = ["idle", "listen", "think", "talk", "happy", "confused", "sad", "sleep", "bored", "wake"]

# Buttons, bit per id
GRAY, YELLOW, GREEN, BLUE, RED = (1 << i for i in range(5))
BUTTON_NAMES = {GRAY: "gray", YELLOW: "yellow", GREEN: "green", BLUE: "blue", RED: "red"}


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def frame(ftype: int, payload: bytes = b"") -> bytes:
    body = bytes([ftype]) + struct.pack("<H", len(payload)) + payload
    return MAGIC + body + bytes([crc8(body)])


class FrameParser:
    """Incremental parser that resyncs on the magic bytes (DIAG text may be
    interleaved on the same port)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                del self.buf[: max(0, len(self.buf) - 1)]  # keep a trailing 0xF7
                return out
            if i:
                del self.buf[:i]
            if len(self.buf) < 5:
                return out
            ftype = self.buf[2]
            (n,) = struct.unpack_from("<H", self.buf, 3)
            if n > MAX_PAYLOAD:
                del self.buf[:2]
                self.bad += 1
                continue
            if len(self.buf) < 6 + n:
                return out
            body = bytes(self.buf[2 : 5 + n])
            if crc8(body) == self.buf[5 + n]:
                out.append((ftype, bytes(self.buf[5 : 5 + n])))
                del self.buf[: 6 + n]
            else:
                self.bad += 1
                del self.buf[:2]


@dataclass
class SpeakerStatus:
    played: int = 0      # samples played since the board booted
    received: int = 0    # samples received since boot
    dropped: int = 0     # received with no room in the ring
    used: int = 0        # samples queued in the ring now
    free: int = 0
    underruns: int = 0
    playing: bool = False
    end: bool = False
    at: float = 0.0      # host time this status arrived


@dataclass
class PowerStatus:
    """The BQ25896 charger's report (wili_proto.h wili_power_t)."""
    ok: bool = False
    vbus_stat: int = 0       # 0 none, 1 USB host, 2 adapter, 7 OTG
    chg_stat: int = 0        # 0 not charging, 1 pre-charge, 2 fast charge, 3 done
    usb_power: bool = False
    fault: int = 0           # 0 ok, 1 input, 2 thermal shutdown, 3 safety timer
    ts_rank: int = 0         # battery thermistor: 0 normal, 2 warm, 3 cool, 5 cold, 6 hot
    thermal_reg: bool = False
    ts_pct: int = 0
    vbat_mv: int = 0
    vsys_mv: int = 0
    vbus_mv: int = 0
    ichg_ma: int = 0
    at: float = 0.0


def find_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID_DISPLAY:
            return p.device
    return None


class WiliBoard:
    def __init__(self, port: str | None = None) -> None:
        port = port or find_port()
        if not port:
            raise RuntimeError("No Wili board found (display CPU 093C:2055). Is the Wili firmware flashed?")
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.05, exclusive=True)
        except serial.SerialException as exc:
            if "lock" in str(exc).lower() or "busy" in str(exc).lower():
                raise RuntimeError("The Wili board is already in use, probably by another copy of Wili.") from exc
            raise
        self.ser.dtr = True
        self.port = port
        self.parser = FrameParser()
        self.mic: queue.Queue[bytes] = queue.Queue(maxsize=2000)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.hello: str | None = None
        self.face_shown: int | None = None
        self.spk = SpeakerStatus()
        self.power: PowerStatus | None = None
        self.speaker_busy_at = 0.0   # last time audio was sent or the board said it was playing
        # (host time, x, y, z in milli-g), newest last; ~100 a second
        self.accel: collections.deque[tuple[float, int, int, int]] = collections.deque(maxlen=1000)
        self.accel_listeners: list = []   # called with each batch from the reader thread
        self.mic_samples = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="wili-reader", daemon=True)
        self._reader.start()
        self.ping()

    # ------------------------------------------------------------ io
    def _send(self, ftype: int, payload: bytes = b"") -> None:
        if ftype in (CMD_AUDIO, CMD_CHIME):
            self.speaker_busy_at = time.time()
        with self._lock:
            self.ser.write(frame(ftype, payload))

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.ser.read(4096)
            except (serial.SerialException, OSError) as exc:
                self.events.put(("error", str(exc)))
                return
            if not data:
                continue
            for ftype, p in self.parser.feed(data):
                if ftype == EV_MIC:
                    self.mic_samples += len(p) // 2
                    try:
                        self.mic.put_nowait(p)
                    except queue.Full:
                        pass
                elif ftype == EV_BUTTON and len(p) >= 3:
                    down, pressed, released = p[0], p[1], p[2]
                    for bit, name in BUTTON_NAMES.items():
                        if pressed & bit:
                            self.events.put(("press", name))
                        if released & bit:
                            self.events.put(("release", name))
                elif ftype == EV_SPK and len(p) >= 20:
                    played, recv, drop, used, free, under, playing, end = struct.unpack_from("<IIIHHHBB", p)
                    self.spk = SpeakerStatus(played, recv, drop, used, free, under, bool(playing), bool(end), time.time())
                    if playing or used:
                        self.speaker_busy_at = self.spk.at
                elif ftype == EV_FACE and p:
                    self.face_shown = p[0]
                elif ftype == EV_ACCEL and len(p) >= 6:
                    n = len(p) // 6
                    now = time.time()
                    batch = [(now - (n - 1 - i) * 0.01, *struct.unpack_from("<hhh", p, i * 6)) for i in range(n)]
                    self.accel.extend(batch)
                    for fn in self.accel_listeners:
                        try:
                            fn(batch)
                        except Exception as exc:  # a listener bug must not kill the reader
                            print(f"  (accel listener failed: {exc})")
                elif ftype == EV_POWER and len(p) >= 16:
                    v = struct.unpack_from("<8B4H", p)
                    self.power = PowerStatus(bool(v[0]), v[1], v[2], bool(v[3]), v[4], v[5], bool(v[6]),
                                             *v[7:], time.time())
                elif ftype == EV_HELLO:
                    self.hello = p.decode(errors="replace")

    def close(self) -> None:
        try:
            self.mic_stream(False)
            self.flush_audio()
            self.face(IDLE)
        except Exception:
            pass
        self._stop.set()
        self._reader.join(timeout=1)
        self.ser.close()

    # ------------------------------------------------------------ commands
    def ping(self, timeout: float = 2.0) -> str | None:
        """PING -> HELLO plus a speaker status, which sets the counter baseline
        that play() paces against."""
        self.hello = None
        before = self.spk.at
        end = time.time() + timeout
        while time.time() < end and (self.hello is None or self.spk.at <= before):
            self._send(CMD_PING)
            t = time.time() + 0.3
            while time.time() < t and (self.hello is None or self.spk.at <= before):
                time.sleep(0.01)
        self._recv_base = self.spk.received
        self._sent = 0
        return self.hello

    def face(self, mood: int) -> None:
        self._send(CMD_FACE, bytes([mood]))

    def chime(self, which: int) -> None:
        """Play a built-in earcon right away (CHIME_LISTEN / CHIME_DONE / CHIME_ERROR)."""
        self._send(CMD_CHIME, bytes([which]))

    def led_brightness(self, percent: float) -> None:
        """Dim the whole LED bar, 0-100 % (firmware 004+; default ~25 %)."""
        self._send(CMD_LED_LEVEL, bytes([max(0, min(255, round(percent * 255 / 100)))]))

    @property
    def firmware(self) -> int:
        """The firmware's version number, from its hello ("WILI 1 008" -> 8); 0 if unknown."""
        try:
            return int((self.hello or "").split()[-1])
        except (ValueError, IndexError):
            return 0

    def rotate(self, quarter_turns: int) -> None:
        """Turn the face 0-3 quarter turns clockwise (firmware 006+; older
        firmware ignores it)."""
        self._send(CMD_ROTATE, bytes([quarter_turns & 3]))

    def caption(self, text: str) -> None:
        """Show a line of text (up to 24 ASCII characters) along the bottom of
        the screen, e.g. "sleeping... 29:41"; "" clears it (firmware 009+;
        older firmware ignores it)."""
        data = text.encode("ascii", "replace")[:CAPTION_MAX]
        self._send(CMD_CAPTION, data)

    def volume(self, level: int) -> None:
        self._send(CMD_VOLUME, bytes([max(0, min(10, level))]))

    def leds(self, colors: list[tuple[int, int, int]] | None) -> None:
        """None = automatic (by mood); else 7 (r,g,b) tuples."""
        if colors is None:
            self._send(CMD_LEDS, b"\x00")
        else:
            self._send(CMD_LEDS, b"\x01" + bytes(v for c in colors[:7] for v in c))

    def mic_stream(self, on: bool) -> None:
        if on:
            while not self.mic.empty():
                self.mic.get_nowait()
        self._send(CMD_MIC, b"\x01" if on else b"\x00")

    def read_mic(self, timeout: float = 0.5) -> list[int]:
        """Next block of mic samples ([] on timeout)."""
        try:
            p = self.mic.get(timeout=timeout)
        except queue.Empty:
            return []
        return list(struct.unpack(f"<{len(p) // 2}h", p))

    def flush_audio(self) -> None:
        self._send(CMD_FLUSH)

    def _queued_on_board(self) -> int:
        """Samples sent that the board hasn't played yet: what's in its ring
        plus what's still in flight over USB."""
        s = self.spk
        in_flight = self._sent - (s.received - self._recv_base)
        return s.used + max(0, in_flight)

    def play(self, pcm: bytes, lead_sec: float = 0.8, final: bool = True, stop: threading.Event | None = None) -> None:
        """Stream 8 kHz int16 LE mono audio to the speaker, keeping about
        `lead_sec` queued on the board (its ring holds ~2 s). Returns once all
        audio is queued and, if `final`, once the board has finished playing it."""
        lead = int(lead_sec * RATE)
        pos = 0
        n = len(pcm) // 2
        while pos < n:
            if stop is not None and stop.is_set():
                self.flush_audio()
                return
            if self._queued_on_board() > lead:
                time.sleep(0.01)
                continue
            chunk = pcm[pos * 2 : pos * 2 + MAX_PAYLOAD]
            self._send(CMD_AUDIO, chunk)
            pos += len(chunk) // 2
            self._sent += len(chunk) // 2
        if final:
            self.end_audio()
            t_end = time.time()
            deadline = t_end + n / RATE + 3.0
            while time.time() < deadline:
                if stop is not None and stop.is_set():
                    self.flush_audio()
                    return
                s = self.spk
                if s.at > t_end and s.received - self._recv_base >= self._sent and s.used == 0 and not s.playing:
                    return
                time.sleep(0.02)

    def end_audio(self) -> None:
        self._send(CMD_AUDIO_END)


if __name__ == "__main__":
    b = WiliBoard()
    print("hello:", b.hello, "on", b.port)
    b.close()
