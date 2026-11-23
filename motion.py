"""Gestures from the Wili board's accelerometer (LIS3DH, 100 Hz, milli-g).

Two things are detected, both tuned on recordings of real handling
(2026-11-21):

* A double tap on the case: two sharp jolts 0.12-0.55 s apart (a third in
  that span is allowed: a hard tap rings), the second at least a quarter as
  strong as the first, after 0.6 s of nothing and followed by 0.35 s of
  nothing, while the board sits still. Real taps
  come from any direction and range from 150 to 1800 mg, so it's the rhythm
  that counts, not the direction: a single tap, a mug set down or handling
  don't match, though a deliberate double knock on the desk does. The board's
  own speaker shakes it as hard as a tap, so impulses are ignored while it's
  playing audio (the caller says when).

* A triple tap: three hits, each within 0.6 s of the one before, after a
  quiet spell and followed by 0.35 s of nothing, while the board sits still.
  (TooGee uses it to wake from a nap; a double tap is too easy to set off by
  bumping the desk.) Three quick taps may also count as a double tap: a
  hard tap's ringing looks like a third hit, so that's allowed there.

* Its pose, from gravity: standing up (y = -1 g), face down (z = -1 g), on
  its back (z = +1 g) or tilted. A pose counts once it has held for 0.8 s.

* Which way up the screen is (0-3 quarter turns clockwise), so the face can
  be turned to match. A rotation counts once it has held for 0.5 s.

Motion.feed() takes samples and returns events; it holds no threads and
does no I/O, so it can be tested on recorded data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

UPRIGHT, FACE_DOWN, ON_BACK, TILTED = "upright", "face_down", "on_back", "tilted"


@dataclass
class _Impulse:
    t: float
    left: int                         # samples still to look at
    still: bool                       # the board was sitting still just before it
    peak: list[int] = field(default_factory=lambda: [0, 0, 0])


class Motion:
    START_MG = 80        # a sample-to-sample change this big starts an impulse
    IMPULSE_SAMPLES = 5  # look at the impulse for 50 ms
    HIT_MG = 150         # an impulse's peak change, on any axis, to count as a hit
    SAME_HIT = 0.12      # a hit rings for ~0.1 s; impulses this soon after are that ringing
    RING_SEC = 0.22      # a hard hit rings longer: a weaker jolt this soon after is ringing too
    RING_FRAC = 0.7      # ("weaker" = under this fraction of the hit)
    GAP_MAX = 0.55       # most time between the two hits of a double tap
    SECOND_MIN = 0.25    # the second hit is at least this fraction of the first
    QUIET_BEFORE = 0.6   # no hit for this long before the first one
    SETTLE = 0.35        # and none for this long after the second
    TRIPLE_GAP = 0.6     # most time between one hit of a triple tap and the next
    STILL_MG = 120       # tilt change over the last second that still counts as sitting still
    POSE_G = 750         # mg of gravity along an axis to call a pose
    POSE_HOLD = 0.8
    ROT_G = 800          # mg of gravity in the screen's plane to pick a rotation
    ROT_HOLD = 0.5

    def __init__(self) -> None:
        self.pose = UPRIGHT
        self.rotation = 0                    # quarter turns clockwise for the face
        self._prev: tuple[int, int, int] | None = None
        self._g = [0.0, -1000.0, 0.0]       # smoothed gravity
        self._g_slow = [0.0, -1000.0, 0.0]  # ... over ~1 s: tells "sitting still" from "being moved"
        self._imp: _Impulse | None = None
        self._hits: list[tuple[float, int]] = []   # (time, peak) of recent hits
        self._last_hit = -10.0
        self._last_peak = 0
        self._cluster_ok = False
        self._pending_double: float | None = None
        self._taps: list[float] = []         # hits of a triple tap so far
        self._pending_triple: float | None = None
        self._pose_cand = UPRIGHT
        self._pose_since = 0.0
        self._rot_cand = 0
        self._rot_since = 0.0

    def feed(self, t: float, x: int, y: int, z: int, quiet: bool = False) -> list[str]:
        """One sample. quiet=True while the speaker plays: impulses are then
        ignored (and cancel any half-finished double tap). Returns events:
        "double_tap", the new pose name, or "rotate" (see .rotation)."""
        out: list[str] = []
        # Judged before this sample moves the averages: a hard tap's own spike
        # would otherwise make the board look like it was being moved.
        still = math.dist(self._g, self._g_slow) < self.STILL_MG
        prev, self._prev = self._prev, (x, y, z)
        for i, v in enumerate((x, y, z)):
            self._g[i] += 0.1 * (v - self._g[i])
            self._g_slow[i] += 0.01 * (v - self._g_slow[i])
        if prev is None:
            return out
        d = (abs(x - prev[0]), abs(y - prev[1]), abs(z - prev[2]))

        if quiet:
            self._imp, self._hits, self._pending_double = None, [], None
            self._taps, self._pending_triple = [], None
            self._last_hit = t
        elif self._imp is not None:
            self._imp.peak = [max(a, b) for a, b in zip(self._imp.peak, d)]
            self._imp.left -= 1
            if self._imp.left <= 0:
                self._impulse_done(self._imp)
                self._imp = None
        elif max(d) >= self.START_MG and t - self._last_hit >= self.SAME_HIT:
            self._imp = _Impulse(t, self.IMPULSE_SAMPLES - 1, still, list(d))

        if self._pending_double is not None and not still:
            self._pending_double, self._hits = None, []   # it's being picked up or moved
        if self._pending_double is not None and t - self._pending_double >= self.SETTLE:
            self._pending_double, self._hits = None, []
            out.append("double_tap")
        if self._pending_triple is not None and not still:
            self._pending_triple, self._taps = None, []
        if self._pending_triple is not None and t - self._pending_triple >= self.SETTLE:
            self._pending_triple, self._taps = None, []
            out.append("triple_tap")

        pose = self._classify()
        if pose != self._pose_cand:
            self._pose_cand, self._pose_since = pose, t
        elif pose != self.pose and t - self._pose_since >= self.POSE_HOLD:
            self.pose = pose
            out.append(pose)

        rot = self._rotation()
        if rot is None or rot != self._rot_cand:
            self._rot_cand, self._rot_since = (self.rotation if rot is None else rot), t
        elif rot != self.rotation and t - self._rot_since >= self.ROT_HOLD:
            self.rotation = rot
            out.append("rotate")
        return out

    def _impulse_done(self, imp: _Impulse) -> None:
        peak = max(imp.peak)
        if peak < self.HIT_MG:
            return                   # ringing, or a nudge
        t = imp.t
        if t - self._last_hit < self.RING_SEC and peak < self.RING_FRAC * self._last_peak:
            return                   # the last hit still ringing
        prev_t = self._last_hit
        self._last_hit, self._last_peak = t, peak
        if not imp.still:
            self._hits, self._pending_double = [], None
            self._taps, self._pending_triple = [], None
            return
        # Triple tap: each hit soon after the one before; the first after a
        # quiet spell. A fourth is banging, not a triple tap.
        if self._taps and t - self._taps[-1] <= self.TRIPLE_GAP:
            self._taps.append(t)
        else:
            self._taps = [t] if t - prev_t >= self.QUIET_BEFORE else []
        self._pending_triple = t if len(self._taps) == 3 else None
        if self._hits and t - self._hits[0][0] <= self.GAP_MAX:
            # Two hits make a double tap; a third that soon is usually a hard
            # tap's ringing, so it's allowed. Any more is banging.
            self._hits.append((t, peak))
            first_peak = self._hits[0][1]
            if len(self._hits) > 3 or not self._cluster_ok:
                self._pending_double = None
            elif peak >= self.SECOND_MIN * first_peak or self._pending_double is not None:
                self._pending_double = t
            return
        if self._hits and self._pending_double is not None:
            self._pending_double = None          # a hit after the cluster: not a double tap
        # A new cluster can open a double tap only after a quiet spell.
        self._cluster_ok = t - prev_t >= self.QUIET_BEFORE
        self._hits = [(t, peak)]

    def _rotation(self) -> int | None:
        """Quarter turns clockwise, from gravity in the screen's plane: standing
        normally y = -1 g; turned clockwise (on its right side) x = +1 g;
        upside down y = +1 g; turned the other way x = -1 g. None while lying
        flat or in between (keep the current one)."""
        gx, gy, _ = self._g
        if gy <= -self.ROT_G:
            return 0
        if gx >= self.ROT_G:
            return 1
        if gy >= self.ROT_G:
            return 2
        if gx <= -self.ROT_G:
            return 3
        return None

    def _classify(self) -> str:
        gx, gy, gz = self._g
        if gz <= -self.POSE_G:
            return FACE_DOWN
        if gz >= self.POSE_G:
            return ON_BACK
        if gy <= -self.POSE_G:
            return UPRIGHT
        return TILTED
