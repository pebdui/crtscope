#!/usr/bin/env python3
"""crtscope - a 1960s military CRT in your terminal, driven by what you're playing.

Left channel moves the beam sideways, right channel moves it up and down, so
stereo music draws Lissajous figures. The beam leaves phosphor glow behind that
fades over time, and it glows brighter where it moves slowly.

Give it an image (crtscope picture.png) and the beam traces the picture's
outlines instead: the music bends the lines, bass makes it pulse, and loudness
makes the beam burn brighter.

Keys (while running):
  p      next phosphor (P7 radar, P1 green, P12 orange, P31 green-short)
  m      XY mode / goniometer mode (mono shows as a vertical line)
  [ ]    shorter / longer afterglow     i / I   dimmer / brighter beam
  + / -  gain (wobble, with an image)   a       auto gain on/off
  g      graticule on/off               o       round tube / square screen
  < >    squash / stretch sideways, if your terminal makes the tube oval
  h      hide the info bar              q / Esc quit
"""

import argparse
import fcntl
import math
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
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
        device = "@DEFAULT_SOURCE@" if use_mic else "@DEFAULT_MONITOR@"
        try:
            self.proc = subprocess.Popen(
                ["parec", f"--device={device}", "--format=float32le",
                 f"--rate={rate}", "--channels=2", "--latency-msec=10"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            sys.exit("crtscope: parec not found (it comes with pipewire-pulse / libpulse)")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        fd = self.proc.stdout.fileno()
        leftover = b""
        while True:
            data = os.read(fd, 65536)
            if not data:
                return
            data = leftover + data
            usable = len(data) // 8 * 8  # one stereo frame = 2 x float32
            leftover = data[usable:]
            frames = np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, 2)
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

    im = ImageOps.exif_transpose(Image.open(path))
    im.thumbnail((240, 240))
    rgba = np.asarray(im.convert("RGBA"), dtype=float) / 255
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
    strong = mag > max(0.25 * np.percentile(mag[mag > 0], 99) if (mag > 0).any() else 1, 0.05)
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
    x -= (x.max() + x.min()) / 2
    y -= (y.max() + y.min()) / 2
    r = np.hypot(x, y).max() or 1
    return x / r * 0.8, y / r * 0.8, blank  # leaves room for bass pulse + wobble


# ----------------------------------------------------------------- main ----


def measure_aspect():
    """Braille dot width / height from the terminal's real cell size in pixels.
    Fonts are rarely exactly 1:2, which would squash the tube into an oval."""
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
        if rows and cols and xpix and ypix:
            return (xpix / cols / 2) / (ypix / rows / 4)
    except OSError:
        pass
    return 1.0  # terminal won't say; assume 1:2 cells (fix with --aspect or < >)


def main():
    ap = argparse.ArgumentParser(description="1960s military CRT audio scope for the terminal")
    ap.add_argument("image", nargs="?", help="image for the beam to trace (png, jpg, ...)")
    ap.add_argument("--rate", type=int, default=192000,
                    help="capture sample rate (default 192000)")
    ap.add_argument("--mic", action="store_true", help="use the microphone instead")
    ap.add_argument("--demo", action="store_true", help="draw a test tone, no audio needed")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--phosphor", type=int, default=1, choices=range(1, len(PHOSPHORS) + 1),
                    help="1=P7 radar 2=P1 green 3=P12 orange 4=P31 green-short")
    ap.add_argument("--aspect", type=float, default=0,
                    help="dot width/height override if the tube isn't round (e.g. 0.9)")
    ap.add_argument("--square", action="store_true", help="square screen instead of round tube")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()

    outline = None
    if args.image:
        try:
            outline = load_outline(args.image)
        except Exception as e:  # unreadable file, not an image, no edges...
            sys.exit(f"crtscope: {args.image}: {e}")
    src = Demo(args.rate) if args.demo else Audio(args.rate, args.mic)
    ph = args.phosphor - 1
    palette = build_palette(PHOSPHORS[ph])
    goniometer = False
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

    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)
    old = termios.tcgetattr(fd) if is_tty else None
    out = sys.stdout
    screen = None
    size = None
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    try:
        if is_tty:
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

            # keys
            while is_tty and select.select([fd], [], [], 0)[0]:
                k = os.read(fd, 1).decode(errors="ignore")
                if k in ("q", "\x1b"):
                    running = False
                elif k == "p":
                    ph = (ph + 1) % len(PHOSPHORS)
                    palette = build_palette(PHOSPHORS[ph])
                elif k == "m":
                    goniometer = not goniometer
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

            cols, rows = shutil.get_terminal_size((100, 30))
            plot_rows = rows - 1 if show_info else rows
            if (cols, plot_rows) != size:
                size = (cols, plot_rows)
                aspect = (args.aspect or measure_aspect()) * nudge
                screen = Screen(cols, plot_rows, round_tube, aspect)
                out.write("\x1b[2J")

            _, _, tau_f, _, tau_g = PHOSPHORS[ph]
            tau_g *= persist
            samples = src.take()[-args.rate // 5:]
            left, right = samples[:, 0], samples[:, 1]
            if goniometer:  # rotate 45 degrees: mono = vertical, out of phase = sideways
                x, y = (left - right) * 0.7071, (left + right) * 0.7071
            else:
                x, y = left, right
            if len(x):
                cur = float(np.max(np.hypot(x, y)))
                peak = max(cur, peak * math.exp(-dt / 1.5), 0.01)
            screen.decay(dt, tau_f, tau_g)
            if outline is None:
                scale = (0.92 / peak if auto else 1.0) * gain * screen.R
                screen.trace(screen.cx + x * scale / screen.aspect, screen.cy - y * scale,
                             src.rate, power, tau_f, tau_g)
            else:
                ox, oy, blank = outline
                n = len(ox)
                # Bass: low-frequency energy against its own recent average
                mono = samples[-16384:].mean(axis=1) if len(samples) else np.zeros(1)
                spec = np.abs(np.fft.rfft(mono))
                freqs = np.fft.rfftfreq(len(mono), 1 / src.rate)
                bass = float(spec[(freqs > 30) & (freqs < 160)].sum())
                loud = float(np.sqrt(np.mean(mono ** 2)))
                bass_avg += (bass - bass_avg) * min(dt / 2.0, 1)
                loud_avg += (loud - loud_avg) * min(dt / 2.0, 1)
                kick = min(max(bass / (bass_avg + 1e-9) - 1, 0), 1.5) if bass > 1e-4 else 0
                bass_env = max(kick, bass_env * math.exp(-dt / 0.18))
                size_k = 1 + 0.15 * bass_env
                bright = min(max(0.35 + 0.65 * loud / (loud_avg + 1e-9), 0.3), 2.0) \
                    if loud > 1e-4 else 0.35
                # The music itself is added to the beam deflection, like feeding
                # the picture and the audio into the same scope inputs
                if len(x):
                    pick = (np.arange(n) * len(x) // n)
                    wx, wy = x[pick] / peak, y[pick] / peak
                else:
                    wx = wy = 0
                wob = 0.05 * gain
                bx = (ox * size_k + wx * wob) * screen.R
                by = (oy * size_k + wy * wob) * screen.R
                lit_len = float(np.hypot(np.diff(bx), np.diff(by))[~blank[1:]].sum())
                screen.last_pt = None
                screen.trace(screen.cx + bx / screen.aspect, screen.cy - by,
                             n / dt, power * bright * max(lit_len, 1) / 700,
                             tau_f, tau_g, blank)

            lines = screen.render(palette, show_grat)
            buf = ["\x1b[H"]
            for r, line in enumerate(lines):
                buf.append("\x1b[%d;1H%s" % (r + 1, line))
            if show_info:
                info = (" CRT-SCOPE │ %s │ %s │ %s %s%.1fx │ glow %.1fs │ beam %d │ %dkHz │ %dfps"
                        "   p m [ ] i I + - a g o < > h q"
                        % (PHOSPHORS[ph][0].strip(),
                           ("IMAGE " + os.path.basename(args.image)) if outline else
                           "GONIO" if goniometer else "XY",
                           "wobble" if outline else "gain",
                           "auto·" if auto and not outline else "",
                           gain if outline else (0.92 / peak if auto else 1) * gain,
                           tau_g, power, src.rate // 1000, fps))
                buf.append("\x1b[%d;1H%s%s\x1b[K" % (rows, fg(INFO_RGB), info[:cols]))
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
