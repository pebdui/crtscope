#!/usr/bin/env python3
"""crtscope - a 1960s military CRT in your terminal, driven by what you're playing.

Left channel moves the beam sideways, right channel moves it up and down, so
stereo music draws Lissajous figures. The beam leaves phosphor glow behind that
fades over time, and it glows brighter where it moves slowly.

Give it an image (crtscope picture.png, or press Tab to pick one from
~/Pictures) and the beam traces the picture's
outlines instead: the music bends the lines, bass makes it pulse, and loudness
makes the beam burn brighter. Give it a GIF or video and it plays every frame
that way ("Bad Apple" mode), with the video's own soundtrack.

Keys (while running):
  m      mode: XY / goniometer / waveform / clock
  p / P  next / previous colour (4 real phosphors, then the 15 Apple II colours)
  [ ]    shorter / longer afterglow     i / I   dimmer / brighter beam
  + / -  gain (wobble, with an image)   a       auto gain on/off
  t / T  waveform: faster / slower sweep
  f      tube flaws: off / subtle / worn (hum, soft edges, flicker)
  g      graticule on/off               o       round tube / square screen
  < >    squash / stretch sideways, if your terminal makes the tube oval
  Tab    browse your pictures and videos and pick one (or go back to the scope)
  h      hide the info bar              q / Esc switch off and quit
"""

import argparse
import bisect
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import ctypes
    import msvcrt
else:
    import fcntl
    import select
    import termios
    import tty

import numpy as np

# ---------------------------------------------------------------- audio ----


class Audio:
    """Reads stereo samples of what your speakers play (or the mic) through parec."""

    def __init__(self, rate, use_mic=False):
        self.rate = rate
        self.chunks = []
        self.pending = 0
        self.lock = threading.Lock()
        self.proc = None
        self.dead = None  # why the audio stopped, if it did (e.g. no sound server)
        device = "@DEFAULT_SOURCE@" if use_mic else "@DEFAULT_MONITOR@"
        try:
            self.proc = subprocess.Popen(
                ["parec", f"--device={device}", "--format=float32le",
                 f"--rate={rate}", "--channels=2", "--latency-msec=10"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            sys.exit("crtscope: parec not found (it comes with pipewire-pulse / libpulse)")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        fd = self.proc.stdout.fileno()
        leftover = b""
        while True:
            data = os.read(fd, 65536)
            if not data:  # parec quit: usually no PipeWire / PulseAudio to listen to
                err = self.proc.stderr.read().decode(errors="ignore").strip().splitlines()
                self.dead = (err[-1] if err else "parec stopped").replace("pa_context_connect() failed: ", "")
                return
            data = leftover + data
            usable = len(data) // 8 * 8  # one stereo frame = 2 x float32
            leftover = data[usable:]
            self._push(np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, 2))

    def _push(self, frames):
        with self.lock:
            self.chunks.append(frames)
            self.pending += len(frames)
            while self.pending > self.rate and len(self.chunks) > 1:  # keep <= 1s
                self.pending -= len(self.chunks.pop(0))

    def take(self):
        with self.lock:
            chunks, self.chunks, self.pending = self.chunks, [], 0
        if not chunks:
            return np.zeros((0, 2), dtype=np.float32)
        return np.concatenate(chunks)

    def close(self):
        if self.proc:
            self.proc.kill()


class WindowsAudio(Audio):
    """Windows: records what the speakers play through WASAPI loopback, using
    the soundcard package (pip install soundcard)."""

    def __init__(self, rate, use_mic=False):
        self.rate = rate
        self.chunks = []
        self.pending = 0
        self.lock = threading.Lock()
        self.proc = None
        self.dead = None
        self.closed = False
        self.use_mic = use_mic
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            import soundcard as sc  # imported here: it sets up Windows audio (COM) for this thread
            if self.use_mic:
                dev = sc.default_microphone()
            else:
                dev = sc.get_microphone(sc.default_speaker().id, include_loopback=True)
            # Windows only records in formats the sound device accepts: try stereo at
            # the usual rates, then mono (some devices, and Wine, only loop back mono)
            rates = [self.rate] + [r for r in (48000, 44100, 96000, 192000, 32000) if r != self.rate]
            tries = [(r, ch) for ch in (2, 1) for r in rates]
            for i, (rate, ch) in enumerate(tries):
                try:
                    rec = dev.recorder(samplerate=rate, channels=ch, blocksize=1024)
                    rec.__enter__()
                    break
                except RuntimeError as e:
                    if "unsupported format" not in str(e) or i == len(tries) - 1:
                        raise
            self.rate = rate
            self.mono = ch == 1
            try:
                while not self.closed:
                    data = np.asarray(rec.record(numframes=1024), dtype=np.float32)
                    if data.ndim == 1 or data.shape[1] == 1:  # mono device: same on both sides
                        data = np.repeat(data.reshape(-1, 1), 2, axis=1)
                    self._push(data[:, :2])
            finally:
                rec.__exit__(None, None, None)
        except ImportError:
            self.dead = "needs the soundcard package: pip install soundcard"
        except Exception as e:  # no loopback device, unsupported rate...
            self.dead = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__

    def close(self):
        self.closed = True


class Demo:
    """A drifting 3:2 test tone, for when there's no audio (or for testing)."""

    def __init__(self, rate):
        self.rate = rate
        self.t = 0.0
        self.last = time.monotonic()

    def take(self):
        now = time.monotonic()
        n = int((now - self.last) * self.rate)
        self.last += n / self.rate
        t = self.t + np.arange(n) / self.rate
        self.t += n / self.rate
        left = 0.6 * np.sin(2 * np.pi * 220 * t)
        right = 0.6 * np.sin(2 * np.pi * 330.4 * t + 0.5)
        return np.column_stack([left, right]).astype(np.float32)

    def close(self):
        pass


# ------------------------------------------------------------ phosphors ----
# Each phosphor has a quick bright "flash" and a slower "glow" afterglow.
# P7 is the two-layer radar phosphor of the era: blue-white flash, long
# yellow-green afterglow.

PHOSPHORS = [
    # name,               flash rgb,        flash s, glow rgb,       glow s
    ("P7  radar",         (170, 195, 255), 0.03,  (175, 255, 50),  1.2),
    ("P1  green",         (190, 255, 190), 0.02,  (40, 255, 70),   0.25),
    ("P12 orange",        (255, 215, 140), 0.04,  (255, 120, 15),  0.8),
    ("P31 green-short",   (215, 255, 215), 0.01,  (70, 255, 120),  0.06),
]

# The Apple II's 16 colours, sampled from a photo of a real composite monitor
# (Black left out: a black beam draws nothing). Brightened by the photo's
# exposure, keeping the dark ones darker, with a whiter flash where the beam lands.
APPLE_II = [
    ("01 Dark Green", (10, 67, 34)), ("02 Royal Blue", (4, 50, 175)),
    ("03 Medium Blue", (53, 159, 231)), ("04 Dark Red", (154, 25, 27)),
    ("05 Gray", (109, 110, 93)), ("06 Purple", (164, 82, 210)),
    ("07 Periwinkle", (132, 174, 239)), ("08 Dark YellowGreen", (54, 39, 21)),
    ("09 Green", (40, 141, 19)), ("10 Gray", (103, 91, 78)),
    ("11 Aquamarine", (83, 175, 162)), ("12 Red", (196, 79, 17)),
    ("13 Yellow", (195, 149, 38)), ("14 Rose", (205, 123, 144)),
    ("15 White", (182, 168, 186)),
]


def apple_phosphor(name, rgb):
    glow = np.array(rgb) * 255 / 186  # the photo's white only reached 186
    if glow.max() < 150:  # keep the darkest ones visible on black
        glow *= 150 / glow.max()
    flash = glow + (255 - glow) * 0.55
    return ("A2 " + name, tuple(flash.clip(0, 255)), 0.02, tuple(glow.clip(0, 255)), 0.35)


PHOSPHORS += [apple_phosphor(n, c) for n, c in APPLE_II]
FLASH_LEVELS, GLOW_LEVELS = 8, 32
GRAT_RGB = (52, 64, 56)
BEZEL_RGB = (70, 70, 74)
INFO_RGB = (110, 130, 115)
BRAILLE = np.array([" "] + [chr(0x2800 + i) for i in range(1, 256)], dtype=object)
# Braille dot (row, col) inside a 2x4 cell -> bit
DOT_BITS = np.array([[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]])


def fg(rgb):
    return "\x1b[38;2;%d;%d;%dm" % tuple(int(c) for c in rgb)


def build_palette(phosphor):
    """Every (flash level, glow level) pair -> one color code, plus graticule/bezel."""
    _, frgb, _, grgb, _ = phosphor
    frgb, grgb = np.array(frgb, float), np.array(grgb, float)
    pal = []
    for fi in range(FLASH_LEVELS):
        f = fi / (FLASH_LEVELS - 1)
        for gi in range(GLOW_LEVELS):
            g = (gi + 1) / GLOW_LEVELS
            rgb = grgb * g ** 0.7 + frgb * f ** 1.2
            peak = rgb.max()
            if peak > 255:  # saturate toward white like an overdriven tube
                rgb = rgb * 255 / peak + (peak - 255) * 0.35
            pal.append(fg(np.clip(rgb, 0, 255)))
    pal += [fg(GRAT_RGB), fg(BEZEL_RGB), ""]
    return np.array(pal, dtype=object)


GRAT_IDX = FLASH_LEVELS * GLOW_LEVELS
BEZEL_IDX = GRAT_IDX + 1
NONE_IDX = GRAT_IDX + 2

# --------------------------------------------------------------- screen ----


class Screen:
    """The tube face at braille-dot resolution (2 dots wide, 4 tall per cell)."""

    def __init__(self, cols, rows, round_tube, aspect):
        self.cols, self.rows = cols, rows
        self.W, self.H = cols * 2, rows * 4
        self.cx, self.cy = self.W / 2, self.H / 2
        # aspect = dot width / dot height on screen. Below 1 means dots are
        # narrow, so a circle needs more dots across than down.
        self.aspect = aspect
        self.R = max(4.0, min(self.W * aspect, self.H) / 2 - 4)  # in dot heights
        self.flash = np.zeros((self.H, self.W))
        self.glow = np.zeros((self.H, self.W))
        self.last_pt = None
        self.sx = self.sy = 1.0   # picture size while the tube powers on / off
        self.flaws = 1            # 0 off, 1 subtle, 2 worn (see FLAWS)
        self.hum_t = 0.0

        yy, xx = np.mgrid[0:self.H, 0:self.W]
        u = (xx + 0.5 - self.cx) * aspect / self.R
        v = (yy + 0.5 - self.cy) / self.R
        rr = np.hypot(u, v)
        if round_tube:
            self.face = rr <= 1.0
            self.bezel = np.abs(rr * self.R - (self.R + 2.5)) < 0.7
        else:
            self.face = (np.abs(u) <= 1.0) & (np.abs(v) <= 1.0)
            edge = np.maximum(np.abs(u), np.abs(v)) * self.R
            self.bezel = np.abs(edge - (self.R + 2.5)) < 0.7
        self.rr = np.clip(rr, 0, 1)
        # Tubes are dimmer toward the rim
        self.vignette = np.clip(1 - 0.45 * rr ** 4, 0.3, 1)

        # Graticule: 10 x 10 divisions, dotted lines, finer ticks on the centre axes
        grat = np.zeros((self.H, self.W), bool)
        div = self.R / 5
        for k in range(-5, 6):
            gx = int(round(self.cx + k * div / aspect))
            gy = int(round(self.cy + k * div))
            if 0 <= gx < self.W:
                grat[::3, gx] = True
            if 0 <= gy < self.H:
                grat[gy, ::3] = True
        icx, icy = int(self.cx), int(self.cy)
        for k in range(-25, 26):
            tx = int(round(self.cx + k * div / 5 / aspect))
            ty = int(round(self.cy + k * div / 5))
            if 0 <= tx < self.W:
                grat[max(icy - 1, 0):icy + 2, tx] = True
            if 0 <= ty < self.H:
                grat[ty, max(icx - 1, 0):icx + 2] = True
        self.grat = grat & self.face

    def decay(self, dt, tau_f, tau_g):
        self.flash *= math.exp(-dt / tau_f)
        self.glow *= math.exp(-dt / tau_g)

    def trace(self, px, py, rate, power, tau_f, tau_g, blank=None):
        """Sweep the beam through the points. Every sample gets the same beam
        time, spread along its path, so slow parts of the trace glow brightest.
        blank[i] switches the beam off while it moves to point i."""
        if len(px) == 0:
            return
        if blank is None:
            blank = np.zeros(len(px), bool)
        px, py = np.asarray(px, float), np.asarray(py, float)
        if self.flaws:
            # an old tube's amplifiers pick up the 60Hz mains hum (a fine ripple
            # up and down) and drift a little sideways as they warm
            t = self.hum_t + np.arange(len(px)) / rate
            self.hum_t = (self.hum_t + len(px) / rate) % 100
            a = (0.0035, 0.009)[self.flaws - 1] * self.R
            py = py + a * np.sin(2 * np.pi * 60 * t)
            px = px + 0.6 * a * np.sin(2 * np.pi * 0.23 * t + 1.3) / self.aspect
        px = self.cx + (px - self.cx) * self.sx
        py = self.cy + (py - self.cy) * self.sy
        if self.last_pt is not None:  # carry on from where the last frame ended
            px = np.concatenate([[self.last_pt[0]], px])
            py = np.concatenate([[self.last_pt[1]], py])
            blank = np.concatenate([[False], blank])
        self.last_pt = (px[-1], py[-1])
        if len(px) < 2:
            px, py = np.repeat(px, 2), np.repeat(py, 2)
            blank = np.repeat(blank, 2)
        dx, dy = np.diff(px), np.diff(py)
        steps = np.clip(np.ceil(np.hypot(dx, dy)), 1, 48).astype(np.int64)
        total = int(steps.sum())
        if total > 400_000:  # loud noise fills the screen; thin it out
            steps = np.maximum(1, steps * 400_000 // total)
            total = int(steps.sum())
        seg = np.repeat(np.arange(len(dx)), steps)
        start = np.repeat(np.cumsum(steps) - steps, steps)
        t = (np.arange(total) - start) / steps[seg]
        x = (px[:-1][seg] + dx[seg] * t).astype(np.int64)
        y = (py[:-1][seg] + dy[seg] * t).astype(np.int64)
        energy = (1.0 / rate) / steps[seg] * ~blank[1:][seg]
        ok = (x >= 0) & (x < self.W) & (y >= 0) & (y < self.H)
        x, y, energy = x[ok], y[ok], energy[ok]
        dep = np.bincount(y * self.W + x, weights=energy, minlength=self.W * self.H)
        dep = dep.reshape(self.H, self.W) * power * (self.R / 100)
        self.flash += dep * (0.7 / tau_f)
        self.glow += dep * (1.0 / tau_g)

    def render(self, palette, show_grat):
        f = (1 - np.exp(-self.flash)) * self.vignette * self.face
        g = (1 - np.exp(-self.glow)) * self.vignette * self.face
        if self.flaws:
            # the beam goes out of focus toward the rim, and the picture flickers
            k = (0.35, 0.8)[self.flaws - 1] * self.rr ** 2

            def soft(a):
                return (a + np.roll(a, 1, 0) + np.roll(a, -1, 0) + np.roll(a, 1, 1) + np.roll(a, -1, 1)) / 5
            f = f + (soft(f) - f) * k
            g = g + (soft(g) - g) * k
            flick = 1 + (0.025, 0.07)[self.flaws - 1] * (np.random.random() * 2 - 1)
            f, g = f * flick, g * flick
        lit = (f + g) > 0.05
        extra = (self.grat if show_grat else False) | self.bezel

        def cells(a, how):
            a = a.reshape(self.rows, 4, self.cols, 2)
            return how(a, axis=(1, 3))

        bits = (lit | extra).reshape(self.rows, 4, self.cols, 2) * DOT_BITS[None, :, None, :]
        code = bits.sum(axis=(1, 3))
        any_lit = cells(lit, np.any)
        fi = np.minimum((cells(f, np.max) * FLASH_LEVELS).astype(int), FLASH_LEVELS - 1)
        gi = np.minimum((cells(g, np.max) * GLOW_LEVELS).astype(int), GLOW_LEVELS - 1)
        idx = np.where(any_lit, fi * GLOW_LEVELS + gi,
                       np.where(cells(self.grat & show_grat, np.any), GRAT_IDX,
                                np.where(cells(self.bezel, np.any), BEZEL_IDX, NONE_IDX)))
        idx = np.where(code == 0, NONE_IDX, idx)

        # Only emit a color code when it differs from the last color used in the row
        has = idx != NONE_IDX
        cols = np.arange(self.cols)
        pos = np.maximum.accumulate(np.where(has, cols, -1), axis=1)
        prev_pos = np.concatenate([np.full((self.rows, 1), -1), pos[:, :-1]], axis=1)
        prev_idx = np.where(prev_pos >= 0,
                            np.take_along_axis(idx, np.maximum(prev_pos, 0), axis=1), -1)
        change = has & (idx != prev_idx)
        tokens = np.where(change, palette[idx] + BRAILLE[code], BRAILLE[code])
        return ["".join(row) for row in tokens]


# ---------------------------------------------------------------- image ----


def load_outline(path, max_points=4000):
    """Turn an image into one beam path along its outlines.
    Returns x, y (fitted inside the unit circle, y up) and a blank mask that is
    True where the beam has to jump to a separate shape with the beam off."""
    from PIL import Image, ImageOps

    im = Image.open(path)
    im.draft("RGB", (480, 480))  # big JPEG photos load much faster
    im = ImageOps.exif_transpose(im)
    im.thumbnail((240, 240))
    return outline_from_rgba(np.asarray(im.convert("RGBA")), max_points)


def outline_from_rgba(rgba, max_points=4000, whole_frame=False):
    """The tracing itself, on an RGBA pixel array (uint8). whole_frame=True
    (video) fits the whole frame in the tube instead of just the shapes, so
    things don't jump around as the picture changes between frames."""
    rgba = rgba.astype(float) / 255
    alpha = rgba[..., 3]
    lum = rgba[..., :3] @ np.array([0.299, 0.587, 0.114]) * alpha

    def edges(a):
        a = np.pad(a, 1, mode="edge")
        a = sum(a[dy:dy + a.shape[0] - 2, dx:dx + a.shape[1] - 2] / 9  # soften
                for dy in range(3) for dx in range(3))
        a = np.pad(a, 1, mode="edge")
        gx = (a[1:-1, 2:] - a[1:-1, :-2]) * 2 + a[:-2, 2:] - a[:-2, :-2] + a[2:, 2:] - a[2:, :-2]
        gy = (a[2:, 1:-1] - a[:-2, 1:-1]) * 2 + a[2:, :-2] - a[:-2, :-2] + a[2:, 2:] - a[:-2, 2:]
        return gx, gy

    gx1, gy1 = edges(lum)
    gx2, gy2 = edges(alpha)
    use2 = np.hypot(gx2, gy2) > np.hypot(gx1, gy1)
    gx, gy = np.where(use2, gx2, gx1), np.where(use2, gy2, gy1)
    mag = np.hypot(gx, gy)

    # Keep only the crest of each edge so lines come out one pixel thin
    m = np.pad(mag, 1)
    h, w = mag.shape
    ang = (np.round(np.arctan2(gy, gx) / (np.pi / 4)) % 4).astype(int)
    nbr = {0: (0, 1), 1: (1, 1), 2: (1, 0), 3: (1, -1)}
    crest = np.zeros_like(mag, bool)
    for a, (dy, dx) in nbr.items():
        fwd = m[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        back = m[1 - dy:1 - dy + h, 1 - dx:1 - dx + w]
        crest |= (ang == a) & (mag >= fwd) & (mag >= back)
    rel = 0.1 if whole_frame else 0.25  # video: keep softer, motion-blurred edges too
    strong = mag > max(rel * np.percentile(mag[mag > 0], 99) if (mag > 0).any() else 1,
                       0.1 if whole_frame else 0.05)
    ys, xs = np.nonzero(crest & strong)
    if len(xs) < 2:
        raise ValueError("couldn't find any outlines in that image")

    # Walk the edge pixels: follow neighbours, jump (beam off) to the nearest
    # unvisited pixel when a line ends.
    where = {(int(y), int(x)): i for i, (y, x) in enumerate(zip(ys, xs))}
    visited = np.zeros(len(xs), bool)
    order, jumps = [], []
    cur = 0
    steps8 = [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    for _ in range(len(xs)):
        visited[cur] = True
        order.append(cur)
        y, x = int(ys[cur]), int(xs[cur])
        nxt = None
        for dy, dx in steps8:
            j = where.get((y + dy, x + dx))
            if j is not None and not visited[j]:
                nxt = j
                break
        if nxt is None:
            left = np.flatnonzero(~visited)
            if not len(left):
                break
            nxt = left[np.argmin((xs[left] - x) ** 2 + (ys[left] - y) ** 2)]
            jumps.append(len(order))
        cur = nxt
    order = np.array(order)
    blank = np.zeros(len(order), bool)
    blank[[j for j in jumps if j < len(order)]] = True

    # Drop specks (strokes shorter than 6 pixels)
    starts = np.concatenate([[0], np.flatnonzero(blank)])
    ends = np.concatenate([starts[1:], [len(order)]])
    keep = np.zeros(len(order), bool)
    for a, b in zip(starts, ends):
        if b - a >= 6:
            keep[a:b] = True
    if keep.sum() >= 2:
        newstart = np.zeros(len(order), bool)
        newstart[starts] = True
        order, newstart = order[keep], newstart[keep]
        blank = newstart
        blank[0] = False

    # Thin out very busy images so the beam can redraw them every frame
    if len(order) > max_points:
        k = math.ceil(len(order) / max_points)
        idx = np.arange(0, len(order), k)
        cs = np.cumsum(blank)
        prev = np.concatenate([[-1], idx[:-1]])
        blank = (cs[idx] - np.where(prev >= 0, cs[np.maximum(prev, 0)], 0)) > 0
        order = order[idx]

    x = xs[order].astype(float)
    y = -ys[order].astype(float)
    if whole_frame:
        x -= (w - 1) / 2
        y += (h - 1) / 2
        r = math.hypot(w, h) / 2 / 1.05  # frame corners just past the rim
        return x / r * 0.8, y / r * 0.8, blank
    x -= (x.max() + x.min()) / 2
    y -= (y.max() + y.min()) / 2
    r = np.hypot(x, y).max() or 1
    return x / r * 0.8, y / r * 0.8, blank  # leaves room for bass pulse + wobble


# ---------------------------------------------------------------- video ----
# "Bad Apple" mode: play a GIF or video by tracing every frame's outlines.

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".gif"}


def is_video(path):
    ext = os.path.splitext(path)[1].lower()
    if ext != ".gif":
        return ext in VIDEO_EXTS
    try:  # a GIF with one frame is just a picture
        from PIL import Image
        return getattr(Image.open(path), "n_frames", 1) > 1
    except Exception:
        return False


def _trace_frame(rgba):
    """Worker-process job: one frame -> beam path (None if the frame is blank)."""
    try:
        x, y, blank = outline_from_rgba(rgba, max_points=2500, whole_frame=True)
    except ValueError:
        return None
    return x.astype(np.float32), y.astype(np.float32), blank


class Video:
    """Decodes frames in the background, traces them on all CPU cores (way
    faster than the video plays), and plays them back on the video's own clock.
    The video's soundtrack is played too, so the music bends its own picture."""

    def __init__(self, path, sound=True):
        self.path = path
        self.name = os.path.basename(path)
        self.frames, self.starts = [], []   # traced frames and when each one starts
        self.total = 0                      # expected frame count, for the loading %
        self.duration = 0.0
        self.done = False
        self.error = None
        self.t = 0.0
        self.playing = False
        self.stalled = False
        self.closed = False
        self.sound = sound and self._has_audio()
        self.sound_proc = None
        threading.Thread(target=self._load, daemon=True).start()

    def _has_audio(self):
        if self.path.lower().endswith(".gif"):
            return False
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                  "-show_entries", "stream=index", "-of", "csv=p=0", self.path],
                                 capture_output=True, text=True, timeout=10).stdout
            return bool(out.strip())
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _decode(self):
        """Yields (rgba uint8 array, seconds on screen) for every frame."""
        if self.path.lower().endswith(".gif"):
            from PIL import Image, ImageSequence
            im = Image.open(self.path)
            self.total = getattr(im, "n_frames", 1)
            for frame in ImageSequence.Iterator(im):
                f = frame.convert("RGBA")
                f.thumbnail((240, 240))
                yield np.asarray(f), max(frame.info.get("duration", 100) or 100, 20) / 1000
            return
        fps = 24
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "format=duration", "-of", "csv=p=0", self.path],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            self.total = int(float(probe) * fps)
        except ValueError:
            pass
        # scale so the long side is 240, keeping both sides even
        vf = f"fps={fps},scale='if(gt(iw,ih),240,-2)':'if(gt(iw,ih),-2,240)'"
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", self.path],
                              capture_output=True, text=True, timeout=10).stdout.strip().split(",")
        w0, h0 = int(size[0]), int(size[1])
        w, h = (240, int(round(240 * h0 / w0 / 2)) * 2) if w0 > h0 else (int(round(240 * w0 / h0 / 2)) * 2, 240)
        proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", self.path, "-vf", vf,
                                 "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while not self.closed:
                data = proc.stdout.read(w * h * 4)
                if len(data) < w * h * 4:
                    break
                yield np.frombuffer(data, np.uint8).reshape(h, w, 4), 1 / fps
        finally:
            proc.kill()

    def _load(self):
        import multiprocessing
        try:
            durations = []

            def frames():
                for rgba, d in self._decode():
                    if self.closed:
                        return
                    durations.append(d)
                    yield rgba

            ctx = multiprocessing.get_context("spawn" if IS_WINDOWS else "forkserver")
            with ctx.Pool(max(1, (os.cpu_count() or 2) - 2)) as pool:
                t = 0.0
                for i, path in enumerate(pool.imap(_trace_frame, frames(), chunksize=4)):
                    if self.closed:
                        break
                    self.starts.append(t)
                    self.frames.append(path)
                    t += durations[i]
                    self.duration = t
        except Exception as e:  # can't decode it, ffmpeg missing, ...
            self.error = str(e) or type(e).__name__
        self.done = True

    def progress(self):
        return len(self.frames) / self.total if self.total else 0.0

    def _sound(self, sig):
        if self.sound_proc and self.sound_proc.poll() is None:
            try:
                self.sound_proc.send_signal(sig)
            except OSError:
                pass

    def _start_sound(self):
        self.stop_sound()
        if self.sound:
            try:
                self.sound_proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", self.path],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.sound = False

    def stop_sound(self):
        if self.sound_proc:
            self.sound_proc.kill()
            self.sound_proc = None

    def frame(self, dt):
        """Advance the clock by dt and return the frame to show: a path, None for
        a blank frame, or "loading" while there's not enough traced to start."""
        if not self.playing:
            # start once a second is traced (or all of it, for short clips)
            if self.frames and (self.done or self.duration >= 1.0):
                self.playing, self.t = True, 0.0
                self._start_sound()
            else:
                return "loading"
        t = self.t + dt
        if self.done and t >= self.duration:  # loop
            t = 0.0
            self._start_sound()
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= len(self.frames) - 1 and not self.done:
            # tracing fell behind (rare): hold this frame and pause the sound
            if not self.stalled:
                if hasattr(signal, "SIGSTOP"):  # Windows can't pause a process; it just plays on
                    self._sound(signal.SIGSTOP)
                self.stalled = True
        else:
            if self.stalled:
                if hasattr(signal, "SIGCONT"):
                    self._sound(signal.SIGCONT)
                self.stalled = False
            self.t = t
        i = max(0, min(bisect.bisect_right(self.starts, self.t) - 1, len(self.frames) - 1))
        return self.frames[i]

    def close(self):
        self.closed = True
        self.stop_sound()


# ----------------------------------------------------------------- text ----
# A single-stroke vector font, like the ones real scope clocks draw: each
# glyph is a few lines on a 4-wide, 6-tall grid (y up).

_G = {
    "0": "0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0 0,1|1,1 3,5",
    "1": "1,5 2,6 2,0|1,0 3,0",
    "2": "0,5 1,6 3,6 4,5 4,4 0,0 4,0",
    "3": "0,5 1,6 3,6 4,5 4,4 3,3 4,2 4,1 3,0 1,0 0,1|1,3 3,3",
    "4": "3,0 3,6 0,2 4,2",
    "5": "4,6 0,6 0,3 3,3 4,2 4,1 3,0 0,0",
    "6": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,2 3,3 0,3",
    "7": "0,6 4,6 1,0",
    "8": "1,3 0,4 0,5 1,6 3,6 4,5 4,4 3,3 1,3 0,2 0,1 1,0 3,0 4,1 4,2 3,3",
    "9": "0,1 1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,4 1,3 4,3",
    "A": "0,0 0,4 2,6 4,4 4,0|0,3 4,3",
    "B": "0,0 0,6 3,6 4,5 4,4 3,3 0,3|3,3 4,2 4,1 3,0 0,0",
    "C": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1",
    "D": "0,0 0,6 2,6 4,4 4,2 2,0 0,0",
    "E": "4,6 0,6 0,0 4,0|0,3 3,3",
    "F": "4,6 0,6 0,0|0,3 3,3",
    "G": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,3 2,3",
    "H": "0,0 0,6|4,0 4,6|0,3 4,3",
    "I": "1,6 3,6|2,6 2,0|1,0 3,0",
    "J": "4,6 4,1 3,0 1,0 0,1",
    "K": "0,0 0,6|4,6 0,2|1,3 4,0",
    "L": "0,6 0,0 4,0",
    "M": "0,0 0,6 2,3 4,6 4,0",
    "N": "0,0 0,6 4,0 4,6",
    "O": "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0",
    "P": "0,0 0,6 3,6 4,5 4,4 3,3 0,3",
    "Q": "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0|2,2 4,0",
    "R": "0,0 0,6 3,6 4,5 4,4 3,3 0,3|2,3 4,0",
    "S": "4,5 3,6 1,6 0,5 0,4 1,3 3,3 4,2 4,1 3,0 1,0 0,1",
    "T": "0,6 4,6|2,6 2,0",
    "U": "0,6 0,1 1,0 3,0 4,1 4,6",
    "V": "0,6 2,0 4,6",
    "W": "0,6 1,0 2,3 3,0 4,6",
    "X": "0,0 4,6|0,6 4,0",
    "Y": "0,6 2,3 4,6|2,3 2,0",
    "Z": "0,6 4,6 0,0 4,0",
    ":": "2,1 2,1.7|2,4.3 2,5",
    "-": "1,3 3,3",
    ".": "2,0 2,0.6",
    ",": "2,0.6 1.4,-0.6",
    "'": "2,6 2,4.8",
    "!": "2,6 2,2|2,0 2,0.6",
    "?": "0,5 1,6 3,6 4,5 4,4 2,3 2,2|2,0 2,0.6",
    "&": "4,0 1,4 1,5 2,6 3,5 0,1 1,0 2,0 4,2",
    "(": "3,6 2,4 2,2 3,0",
    ")": "1,6 2,4 2,2 1,0",
    "/": "0,0 4,6",
    "+": "2,1 2,5|0,3 4,3",
    "%": "0,0 4,6|0.5,5 0.5,5.6|3.5,0.4 3.5,1",
    "#": "1,0 1,6|3,0 3,6|0,2 4,2|0,4 4,4",
}
GLYPHS = {ch: [np.array([[float(v) for v in p.split(",")] for p in stroke.split()])
               for stroke in spec.split("|")] for ch, spec in _G.items()}
ADVANCE = 6.0  # glyph width 4 + gap 2


def _ascii(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c if c.upper() in GLYPHS else " " for c in text).upper()


def text_width(text, height):
    return max(len(_ascii(text)) * ADVANCE - 2, 0) * height / 6


def text_path(text, height, cx=0.0, cy=0.0, spacing=0.006):
    """Beam path that writes text centred on (cx, cy), in tube-radius units.
    Points are spread evenly along the strokes so every line burns equally."""
    text = _ascii(text)
    k = height / 6
    x0 = cx - text_width(text, height) / 2
    xs, ys, blank = [], [], []
    for i, ch in enumerate(text):
        for stroke in GLYPHS.get(ch, []):
            pts = stroke * k + [x0 + i * ADVANCE * k, cy - height / 2]
            seg = np.hypot(*np.diff(pts, axis=0).T)
            n = np.maximum(np.ceil(seg / spacing).astype(int), 1)
            for (a, b), m in zip(zip(pts[:-1], pts[1:]), n):
                t = np.arange(m)[:, None] / m
                p = a + (b - a) * t
                xs.append(p[:, 0]); ys.append(p[:, 1])
                blank.append(np.zeros(m, bool))
            xs.append(pts[-1:, 0]); ys.append(pts[-1:, 1]); blank.append(np.zeros(1, bool))
            blank[-1 - (len(n))][0] = True  # beam off while jumping to this stroke
    if not xs:
        return np.zeros(0), np.zeros(0), np.zeros(0, bool)
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(blank)


def join_paths(*paths):
    paths = [p for p in paths if p is not None and len(p[0])]
    if not paths:
        return None
    x = np.concatenate([p[0] for p in paths])
    y = np.concatenate([p[1] for p in paths])
    b = np.concatenate([p[2] for p in paths])
    return x, y, b


class NowPlaying:
    """The song title from any media player that reports it to the desktop
    (Spotify, browsers, mpv... over MPRIS), checked every 2 seconds."""

    def __init__(self):
        self.text = ""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            import gi
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            return  # no D-Bus / PyGObject: just no title

        def get(name, prop):
            r = bus.call_sync(name, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties",
                              "Get", GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", prop)),
                              GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 500, None)
            return r.unpack()[0]

        while True:
            try:
                names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                      "org.freedesktop.DBus", "ListNames", None,
                                      GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 500,
                                      None).unpack()[0]
                best = ""
                for name in (n for n in names if n.startswith("org.mpris.MediaPlayer2.")):
                    try:
                        meta = get(name, "Metadata")
                        status = get(name, "PlaybackStatus")
                    except Exception:
                        continue
                    title = meta.get("xesam:title", "")
                    artist = ", ".join(meta.get("xesam:artist", []) or [])
                    line = f"{artist} - {title}" if artist and title else title
                    if line and (status == "Playing" or not best):
                        best = line
                        if status == "Playing":
                            break
                self.text = best
            except Exception:
                self.text = ""
            time.sleep(2)


def clock_path(now_playing, elapsed):
    """Time in big strokes, the date above, and what's playing below. A title
    too long to fit is shown a page at a time (scrolling text would smear in
    the phosphor's afterglow)."""
    lt = time.localtime()
    parts = [text_path(time.strftime("%H:%M:%S", lt), 0.2),
             text_path(time.strftime("%a %d %b %Y", lt), 0.09, cy=0.36)]
    song = now_playing.text if now_playing else ""
    if song:
        h, room = 0.09, 1.45
        pages, line = [], ""
        for word in _ascii(song).split():
            while text_width(word, h) > room:  # a single huge word: chop it
                cut = int(room / (ADVANCE * h / 6))
                if line:
                    pages.append(line)
                    line = ""
                pages.append(word[:cut])
                word = word[cut:]
            if line and text_width(line + " " + word, h) > room:
                pages.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            pages.append(line)
        if pages:
            parts.append(text_path(pages[int(elapsed / 3) % len(pages)], h, cy=-0.38))
    return join_paths(*parts)


def trigger(sig, win, prev=None):
    """Scope trigger: start the window where the wave last rose through half
    its height (leaving a full window after it), so repeating waves stand
    still. Half height, not zero: busy waves cross zero several times per
    cycle, which would make copies of the wave pile up."""
    if len(sig) <= win + 2:
        return sig[-win:]
    k = 24  # smooth a little so noise doesn't trigger it
    sm = np.convolve(sig, np.ones(k) / k, mode="same")
    level = 0.5 * float(sm.max())
    cand = np.flatnonzero((sm[:-1] < level) & (sm[1:] >= level)) + 1
    cand = cand[cand <= len(sig) - win]
    if not len(cand):
        return sig[-win:]
    if prev is not None and len(prev) == win and len(cand) > 1:
        # several places look like a trigger point: take the one whose wave
        # best matches what's on screen, so the picture holds still
        cand = cand[-24:]
        step = max(1, win // 400)
        ref = prev[::step]
        err = [float(np.mean((sig[c:c + win:step] - ref) ** 2)) for c in cand]
        start = cand[int(np.argmin(err))]
    else:
        start = cand[-1]
    return sig[start:start + win]


def power_curve(on_t, off_t):
    """Horizontal size, vertical size and beam strength while the tube switches
    on (a dot that opens into a line, then the full picture) or off (the
    picture collapses to a line, then to a dot that slowly fades)."""
    def ease(a):
        a = min(max(a, 0.0), 1.0)
        return a * a * (3 - 2 * a)
    if off_t is not None:
        sy = 1 - ease(off_t / 0.18)
        sx = 1 - ease((off_t - 0.18) / 0.22)
        fade = 1.0 if off_t < 0.45 else math.exp(-(off_t - 0.45) / 0.35)
        return max(sx, 0.0), max(sy, 0.004), fade
    if on_t >= 0.9:
        return 1.0, 1.0, 1.0
    sx = ease((on_t - 0.15) / 0.3)
    sy = max(ease((on_t - 0.45) / 0.4), 0.004)
    return sx, sy, min(1.0, 0.3 + on_t * 2)


FLAWS = ["off", "subtle", "worn"]

# --------------------------------------------------------------- picker ----

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"} | VIDEO_EXTS


class Picker:
    """The Tab menu: browse folders and choose an image for the beam to trace."""

    def __init__(self, start, have_image):
        self.have_image = have_image
        self.msg = ""
        self.dir = None
        self.open_dir(start)

    def open_dir(self, path, select_name=None):
        try:
            names = sorted(os.listdir(path), key=str.lower)
        except OSError as e:
            self.msg = "can't open %s: %s" % (os.path.basename(path) or path, e.strerror)
            return
        self.dir = path
        full = [(n, os.path.join(path, n)) for n in names if not n.startswith(".")]
        dirs = [(n, f) for n, f in full if os.path.isdir(f)]
        imgs = [(n, f) for n, f in full
                if os.path.splitext(n)[1].lower() in IMAGE_EXTS and os.path.isfile(f)]
        self.entries = []
        if self.have_image:
            self.entries.append(("scope", None, "◉ back to the scope (no image or video)"))
        if os.path.dirname(path) != path:
            self.entries.append(("dir", os.path.dirname(path), "◂ .."))
        self.entries += [("dir", f, n + "/") for n, f in dirs]
        self.entries += [("image", f, ("▶ " if os.path.splitext(n)[1].lower() in VIDEO_EXTS - {".gif"}
                                       else "") + n) for n, f in imgs]
        self.sel, self.top = 0, 0
        for i, (kind, f, _) in enumerate(self.entries):
            if select_name and f and os.path.basename(f) == select_name:
                self.sel = i
        if not imgs and not dirs:
            self.msg = "no images in this folder"

    def key(self, k):
        """Returns ("close",), ("scope",), ("pick", path) or None."""
        n = len(self.entries)
        if k in ("TAB", "ESC"):
            return ("close",)
        if k in ("UP", "k"):
            self.sel = (self.sel - 1) % n
        elif k in ("DOWN", "j"):
            self.sel = (self.sel + 1) % n
        elif k == "PGUP":
            self.sel = max(self.sel - 10, 0)
        elif k == "PGDN":
            self.sel = min(self.sel + 10, n - 1)
        elif k in ("LEFT", "BACKSPACE"):
            self.msg = ""
            self.open_dir(os.path.dirname(self.dir), os.path.basename(self.dir))
        elif k in ("RIGHT", "ENTER", " "):
            kind, path, _ = self.entries[self.sel]
            if kind == "dir":
                self.msg = ""
                came_from = os.path.basename(self.dir) if path == os.path.dirname(self.dir) else None
                self.open_dir(path, came_from)
            elif kind == "image" and k != "RIGHT":
                return ("pick", path)
            elif kind == "scope" and k != "RIGHT":
                return ("scope",)
        return None

    def draw(self, cols, rows):
        """The menu box as escape codes, drawn on top of the tube."""
        w = max(20, min(64, cols - 4))
        h = max(3, min(len(self.entries), rows - 8))
        x0 = (cols - w) // 2 + 1
        y0 = max(1, (rows - (h + 4)) // 2 + 1)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + h:
            self.top = self.sel - h + 1
        bg, border = "\x1b[48;2;8;12;10m", fg(INFO_RGB)
        home = os.path.expanduser("~")
        where = ("~" + self.dir[len(home):]) if self.dir.startswith(home) else self.dir
        title = (" " + where + " ")[-(w - 4):]
        lines = [border + "╭─" + title + "─" * (w - 3 - len(title)) + "╮"]
        for i in range(self.top, self.top + h):
            if i < len(self.entries):
                kind, _, label = self.entries[i]
                color = fg((150, 200, 255) if kind == "dir" else
                           (255, 200, 120) if kind == "scope" else (200, 235, 205))
                text = (" " + label)[:w - 2].ljust(w - 2)
                if i == self.sel:
                    text = "\x1b[7m" + text + "\x1b[27m"
                lines.append(border + "│" + color + text + border + "│")
            else:
                lines.append(border + "│" + " " * (w - 2) + "│")
        foot = self.msg or "↑↓ choose  Enter pick  ← back  Tab close"
        lines.append(border + "│" + fg((230, 160, 120) if self.msg else INFO_RGB)
                     + (" " + foot)[:w - 2].ljust(w - 2) + border + "│")
        lines.append(border + "╰" + "─" * (w - 2) + "╯")
        return "".join("\x1b[%d;%dH%s%s" % (y0 + r, x0, bg, line)
                       for r, line in enumerate(lines)) + "\x1b[49m"


KEY_SEQS = {"\x1b[A": "UP", "\x1b[B": "DOWN", "\x1b[C": "RIGHT", "\x1b[D": "LEFT",
            "\x1bOA": "UP", "\x1bOB": "DOWN", "\x1bOC": "RIGHT", "\x1bOD": "LEFT",
            "\x1b[5~": "PGUP", "\x1b[6~": "PGDN"}
KEY_NAMES = {"\r": "ENTER", "\n": "ENTER", "\t": "TAB", "\x7f": "BACKSPACE", "\x08": "BACKSPACE"}


WIN_KEYS = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "I": "PGUP", "Q": "PGDN"}


def read_keys_windows():
    """Windows console: special keys arrive as a prefix character plus a code."""
    keys = []
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            name = WIN_KEYS.get(msvcrt.getwch())
            if name:
                keys.append(name)
        elif ch == "\x1b":
            keys.append("ESC")
        elif ch == "\x03":
            raise KeyboardInterrupt
        else:
            keys.append(KEY_NAMES.get(ch, ch))
    return keys


def read_keys(fd):
    """Everything typed since last frame, with arrow keys etc. as one key each
    (they arrive as Esc + letters, which must not count as a lone Esc)."""
    if IS_WINDOWS:
        return read_keys_windows()
    data = b""
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 1024)
        if not chunk:
            break
        data += chunk
    s, keys, i = data.decode(errors="ignore"), [], 0
    while i < len(s):
        if s[i] == "\x1b":
            seq = next((q for q in KEY_SEQS if s.startswith(q, i)), None)
            if seq:
                keys.append(KEY_SEQS[seq])
                i += len(seq)
            elif s.startswith("\x1b[", i):  # some other special key: skip it
                j = i + 2
                while j < len(s) and not (s[j].isalpha() or s[j] == "~"):
                    j += 1
                i = j + 1
            else:
                keys.append("ESC")
                i += 1
            continue
        keys.append(KEY_NAMES.get(s[i], s[i]))
        i += 1
    return keys


# ----------------------------------------------------------------- main ----


def measure_aspect():
    """Braille dot width / height from the terminal's real cell size in pixels.
    Fonts are rarely exactly 1:2, which would squash the tube into an oval."""
    if IS_WINDOWS:
        try:  # the console font's cell size
            class COORD(ctypes.Structure):
                _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

            class FONT(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("nFont", ctypes.c_ulong), ("dwFontSize", COORD),
                            ("FontFamily", ctypes.c_uint), ("FontWeight", ctypes.c_uint),
                            ("FaceName", ctypes.c_wchar * 32)]
            f = FONT()
            f.cbSize = ctypes.sizeof(FONT)
            h = ctypes.windll.kernel32.GetStdHandle(-11)
            if ctypes.windll.kernel32.GetCurrentConsoleFontEx(h, False, ctypes.byref(f)) \
                    and f.dwFontSize.X and f.dwFontSize.Y:
                return (f.dwFontSize.X / 2) / (f.dwFontSize.Y / 4)
        except Exception:
            pass
        return 1.0
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
        if rows and cols and xpix and ypix:
            return (xpix / cols / 2) / (ypix / rows / 4)
    except OSError:
        pass
    return 1.0  # terminal won't say; assume 1:2 cells (fix with --aspect or < >)


MODES = ["xy", "gonio", "wave", "clock"]
MODE_NAMES = {"xy": "XY", "gonio": "GONIO", "wave": "WAVE", "clock": "CLOCK"}


def main():
    ap = argparse.ArgumentParser(description="1960s military CRT audio scope for the terminal")
    ap.add_argument("image", nargs="?",
                    help="image to trace (png, jpg, ...) or a GIF / video to play as outlines")
    ap.add_argument("--rate", type=int, default=48000 if IS_WINDOWS else 192000,
                    help="capture sample rate (default 192000; 48000 on Windows)")
    ap.add_argument("--mic", action="store_true", help="use the microphone instead")
    ap.add_argument("--demo", action="store_true", help="draw a test tone, no audio needed")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--mode", choices=MODES, default="xy",
                    help="xy (Lissajous), gonio, wave (classic waveform) or clock")
    ap.add_argument("--phosphor", type=int, default=1, choices=range(1, len(PHOSPHORS) + 1),
                    help="1=P7 radar 2=P1 green 3=P12 orange 4=P31 green-short, "
                         "5-19 = Apple II colours 01-15 (Dark Green ... White)")
    ap.add_argument("--flaws", choices=FLAWS, default="subtle",
                    help="tube imperfections: hum, soft edges, flicker (default subtle)")
    ap.add_argument("--mute", action="store_true", help="don't play a video's own soundtrack")
    ap.add_argument("--no-power", action="store_true", help="skip the power on / off effect")
    ap.add_argument("--aspect", type=float, default=0,
                    help="dot width/height override if the tube isn't round (e.g. 0.9)")
    ap.add_argument("--square", action="store_true", help="square screen instead of round tube")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()

    outline = None
    video = None
    image_name = os.path.basename(args.image) if args.image else None
    browse_dir = (os.path.dirname(os.path.abspath(args.image)) if args.image else
                  next((d for d in (os.path.expanduser("~/Pictures"), os.path.expanduser("~"))
                        if os.path.isdir(d)), os.getcwd()))
    picker = None
    if args.image:
        if not os.path.isfile(args.image):
            sys.exit(f"crtscope: {args.image}: no such file")
        if is_video(args.image):
            video = Video(args.image, sound=not args.mute)
        else:
            try:
                outline = load_outline(args.image)
            except Exception as e:  # unreadable file, not an image, no edges...
                sys.exit(f"crtscope: {args.image}: {e}")
    if args.demo:
        src = Demo(args.rate)
    elif IS_WINDOWS:
        src = WindowsAudio(args.rate, args.mic)
    else:
        src = Audio(args.rate, args.mic)
    now_playing = NowPlaying()
    ph = args.phosphor - 1
    palette = build_palette(PHOSPHORS[ph])
    mode = args.mode
    flaws = FLAWS.index(args.flaws)
    timebase = 0.02   # seconds across the screen in wave mode
    persist = 1.0
    power = 900.0
    gain = 1.0
    auto = True
    show_grat = True
    round_tube = not args.square
    nudge = 1.0  # manual fine-tune on top of the measured aspect
    show_info = True
    peak = 0.05
    bass_avg = loud_avg = 1e-6
    bass_env = 0.0
    on_t = 0.0 if not args.no_power else 10.0
    off_t = None      # seconds since you pressed quit (the picture collapses)
    scroll = 0.0
    last_wave = None  # the wave on screen, so the trigger can hold it still
    wave_buf = np.zeros(0, np.float32)  # recent sound, so the trigger has room to search

    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)
    old = termios.tcgetattr(fd) if is_tty and not IS_WINDOWS else None
    if IS_WINDOWS:
        try:  # let the console understand colour/cursor escape codes, and print braille
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            con = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(con)):
                k32.SetConsoleMode(h, con.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            k32.SetConsoleOutputCP(65001)
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    out = sys.stdout
    screen = None
    size = None
    running = True

    def stop(*_):
        nonlocal running
        running = False

    def drop_media():
        nonlocal outline, video, image_name
        if video:
            video.close()
        outline = video = image_name = None

    signal.signal(signal.SIGTERM, stop)
    try:
        if is_tty and not IS_WINDOWS:
            tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        frame_time = 1.0 / args.fps
        last = time.monotonic()
        fps = 0.0
        frames = 0
        while running:
            now = time.monotonic()
            dt = max(now - last, 1e-4)
            last = now
            fps = fps * 0.9 + 0.1 / dt
            on_t += dt
            scroll += dt

            # keys
            for k in (read_keys(fd) if is_tty else []):
                if off_t is not None:  # a key while it's switching off: just go
                    running = False
                    break
                if picker:
                    act = picker.key(k)
                    if act:
                        browse_dir = picker.dir
                    if act == ("close",):
                        picker = None
                    elif act == ("scope",):
                        drop_media()
                        picker = None
                        screen.last_pt = None
                    elif act and act[0] == "pick":
                        try:
                            if is_video(act[1]):
                                drop_media()
                                video = Video(act[1], sound=not args.mute)
                            else:
                                new = load_outline(act[1])
                                drop_media()
                                outline = new
                            image_name = os.path.basename(act[1])
                            picker = None
                        except Exception as e:  # not an image, no edges...
                            picker.msg = "can't use that one: %s" % e
                    continue
                if k == "TAB":
                    picker = Picker(browse_dir, outline is not None or video is not None)
                elif k in ("q", "ESC"):
                    if args.no_power:
                        running = False
                    else:
                        off_t = 0.0
                        if video:
                            video.stop_sound()
                elif k in ("p", "P"):
                    ph = (ph + (1 if k == "p" else -1)) % len(PHOSPHORS)
                    palette = build_palette(PHOSPHORS[ph])
                elif k == "m":
                    mode = MODES[(MODES.index(mode) + 1) % len(MODES)]
                    screen.last_pt = None
                elif k == "t":
                    timebase = max(timebase / 1.5, 0.001)
                elif k == "T":
                    timebase = min(timebase * 1.5, 0.2)
                elif k == "f":
                    flaws = (flaws + 1) % len(FLAWS)
                elif k == "]":
                    persist = min(persist * 1.4, 20)
                elif k == "[":
                    persist = max(persist / 1.4, 0.05)
                elif k == "I":
                    power = min(power * 1.3, 50000)
                elif k == "i":
                    power = max(power / 1.3, 20)
                elif k in ("+", "="):
                    gain = min(gain * 1.2, 50)
                elif k == "-":
                    gain = max(gain / 1.2, 0.02)
                elif k == "a":
                    auto = not auto
                elif k == "g":
                    show_grat = not show_grat
                elif k == "o":
                    round_tube = not round_tube
                    size = None
                elif k in ("<", ","):
                    nudge /= 1.02
                    size = None
                elif k in (">", "."):
                    nudge *= 1.02
                    size = None
                elif k == "h":
                    show_info = not show_info
                    size = None
            if off_t is not None:
                off_t += dt
                if off_t > 1.6:
                    break

            cols, rows = shutil.get_terminal_size((100, 30))
            plot_rows = rows - 1 if show_info else rows
            if (cols, plot_rows) != size:
                size = (cols, plot_rows)
                aspect = (args.aspect or measure_aspect()) * nudge
                screen = Screen(cols, plot_rows, round_tube, aspect)
                out.write("\x1b[2J")
            screen.flaws = flaws
            screen.sx, screen.sy, beam = power_curve(on_t, off_t)

            _, _, tau_f, _, tau_g = PHOSPHORS[ph]
            tau_g *= persist
            if video and video.playing:
                tau_g = min(tau_g, 0.12)  # long afterglow would smear the frames together
            elif mode == "clock" and outline is None and video is None:
                tau_g = min(tau_g, 0.35)  # keep the ticking seconds and scrolling title readable
            samples = src.take()[-args.rate // 5:]
            left, right = samples[:, 0], samples[:, 1]
            if mode == "gonio":  # rotate 45 degrees: mono = vertical, out of phase = sideways
                x, y = (left - right) * 0.7071, (left + right) * 0.7071
            else:
                x, y = left, right
            if len(x):
                cur = float(np.max(np.hypot(x, y)))
                peak = max(cur, peak * math.exp(-dt / 1.5), 0.01)
            screen.decay(dt, tau_f, tau_g)

            # What the beam draws: a path (video frame, image, clock text), or the sound itself
            path, status = None, ""
            if video:
                fr = video.frame(dt)
                if video.error and not video.frames:
                    path = text_path("CAN'T PLAY THAT", 0.09)
                    status = "VIDEO " + video.name + " (error: " + video.error + ")"
                elif fr == "loading":
                    path = text_path("TRACING %d%%" % (100 * video.progress()), 0.11)
                    status = "VIDEO " + video.name + " (tracing)"
                else:
                    path = fr
                    status = "VIDEO %s %d:%02d%s" % (video.name, int(video.t) // 60, int(video.t) % 60,
                                                     " (tracing…)" if video.stalled else "")
            elif outline:
                path, status = outline, "IMAGE " + image_name
            elif mode == "clock":
                path, status = clock_path(now_playing, scroll), "CLOCK"
            elif getattr(src, "dead", None):
                # nothing to listen to: say so instead of showing an empty tube
                path = join_paths(text_path("NO SIGNAL", 0.14), text_path("TRY --DEMO", 0.07, cy=-0.25))
                status = "NO AUDIO: %s (%s try --demo)" % (
                    src.dead, "are your speakers enabled in Sound settings?" if IS_WINDOWS
                    else "is PipeWire / PulseAudio running?")
            else:
                status = MODE_NAMES[mode]

            if path is not None or video:
                if path is not None and len(path[0]):
                    ox, oy, blank = path
                    n = len(ox)
                    # Bass: low-frequency energy against its own recent average
                    mono = samples[-16384:].mean(axis=1) if len(samples) else np.zeros(1)
                    spec = np.abs(np.fft.rfft(mono))
                    freqs = np.fft.rfftfreq(len(mono), 1 / src.rate)
                    bass = float(spec[(freqs > 30) & (freqs < 160)].sum())
                    loud = float(np.sqrt(np.mean(mono ** 2)))
                    if bass_avg <= 1e-6 and bass > 1e-4:  # first real sound: start from it,
                        bass_avg, loud_avg = bass, loud      # or everything looks like a bass hit
                    bass_avg += (bass - bass_avg) * min(dt / 2.0, 1)
                    loud_avg += (loud - loud_avg) * min(dt / 2.0, 1)
                    kick = min(max(bass / (bass_avg + 1e-9) - 1, 0), 1.5) if bass > 1e-4 else 0
                    bass_env = max(kick, bass_env * math.exp(-dt / 0.18))
                    size_k = 1 + 0.15 * bass_env
                    bright = min(max(0.35 + 0.65 * loud / (loud_avg + 1e-9), 0.3), 2.0) \
                        if loud > 1e-4 else 0.35
                    if outline is None and video is None:
                        bright = max(bright, 0.8)  # clock stays readable in silence
                    # The music itself is added to the beam deflection, like feeding
                    # the picture and the audio into the same scope inputs
                    if len(x):
                        pick = (np.arange(n) * len(x) // n)
                        wx, wy = x[pick] / peak, y[pick] / peak
                    else:
                        wx = wy = 0
                    # how far the music wiggles the lines: less for text and video
                    # frames, where too much smears the detail into blobs
                    wob = (0.05 if outline is not None else 0.015 if video else 0.008) * gain
                    bx = (ox * size_k + wx * wob) * screen.R
                    by = (oy * size_k + wy * wob) * screen.R
                    lit_len = float(np.hypot(np.diff(bx), np.diff(by))[~blank[1:]].sum())
                    screen.last_pt = None
                    screen.trace(screen.cx + bx / screen.aspect, screen.cy - by,
                                 n / dt, power * bright * beam * max(lit_len, 1) / 700,
                                 tau_f, tau_g, blank)
            elif mode == "wave":
                # Classic scope: the sound wave sweeping left to right, triggered so it stands still
                win = max(int(timebase * src.rate), 16)
                keep = max(3 * win, src.rate // 4)
                wave_buf = np.concatenate([wave_buf, (left + right) / 2])[-keep:]
                mono = wave_buf
                seg = trigger(mono, win, last_wave) if len(mono) else np.zeros(0)
                last_wave = seg
                if len(seg) > 1:
                    wpeak = max(float(np.max(np.abs(mono))), 0.01)
                    scale = (0.8 / wpeak if auto else 1.0) * gain * screen.R
                    sweep = np.linspace(-1, 1, len(seg)) * screen.R
                    screen.last_pt = None  # the beam is blanked while it flies back
                    screen.trace(screen.cx + sweep / screen.aspect, screen.cy - seg * scale,
                                 src.rate, power * beam * min(dt / timebase, 8), tau_f, tau_g)
                status = "WAVE %.1fms/div" % (timebase * 100)
            else:
                scale = (0.92 / peak if auto else 1.0) * gain * screen.R
                screen.trace(screen.cx + x * scale / screen.aspect, screen.cy - y * scale,
                             src.rate, power * beam, tau_f, tau_g)

            lines = screen.render(palette, show_grat)
            buf = ["\x1b[H"]
            for r, line in enumerate(lines):
                buf.append("\x1b[%d;1H%s" % (r + 1, line))
            if show_info:
                xy = path is None and mode in ("xy", "gonio")
                info = (" CRT-SCOPE │ %s │ %s │ %s %s%.1fx │ glow %.1fs │ beam %d │ flaws %s │ %dkHz │ %dfps"
                        "   Tab images/videos  m p [ ] i I + - t T f a g o < > h q"
                        % (PHOSPHORS[ph][0].strip(), status,
                           "gain" if xy or mode == "wave" and path is None else "wobble",
                           "auto·" if auto and path is None else "",
                           (0.92 / peak if auto else 1) * gain if xy else gain,
                           tau_g, power, FLAWS[flaws], src.rate // 1000, fps))
                buf.append("\x1b[%d;1H%s%s\x1b[K" % (rows, fg(INFO_RGB), info[:cols]))
            if picker:
                buf.append(picker.draw(cols, plot_rows))
            out.write("".join(buf))
            out.flush()

            frames += 1
            if args.frames and frames >= args.frames:
                break
            spare = frame_time - (time.monotonic() - now)
            if spare > 0:
                time.sleep(spare)
    finally:
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        src.close()
        if video:
            video.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
