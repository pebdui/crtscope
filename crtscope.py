#!/usr/bin/env python3
"""crtscope - a 1960s military CRT in your terminal, driven by what you're playing.

Left channel moves the beam sideways, right channel moves it up and down, so
stereo music draws Lissajous figures. The beam leaves phosphor glow behind that
fades over time, and it glows brighter where it moves slowly.

Keys (while running):
  p      next phosphor (P7 radar, P1 green, P12 orange, P31 green-short)
  m      XY mode / goniometer mode (mono shows as a vertical line)
  [ ]    shorter / longer afterglow     i / I   dimmer / brighter beam
  + / -  gain                           a       auto gain on/off
  g      graticule on/off               o       round tube / square screen
  h      hide the info bar              q / Esc quit
"""

import argparse
import math
import os
import select
import shutil
import signal
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

    def __init__(self, cols, rows, round_tube):
        self.cols, self.rows = cols, rows
        self.W, self.H = cols * 2, rows * 4
        self.cx, self.cy = self.W / 2, self.H / 2
        self.R = max(4.0, min(self.W, self.H) / 2 - 4)
        self.flash = np.zeros((self.H, self.W))
        self.glow = np.zeros((self.H, self.W))
        self.last_pt = None

        yy, xx = np.mgrid[0:self.H, 0:self.W]
        u = (xx + 0.5 - self.cx) / self.R
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
            gx = int(round(self.cx + k * div))
            gy = int(round(self.cy + k * div))
            if 0 <= gx < self.W:
                grat[::3, gx] = True
            if 0 <= gy < self.H:
                grat[gy, ::3] = True
        icx, icy = int(self.cx), int(self.cy)
        for k in range(-25, 26):
            tx = int(round(self.cx + k * div / 5))
            ty = int(round(self.cy + k * div / 5))
            if 0 <= tx < self.W:
                grat[max(icy - 1, 0):icy + 2, tx] = True
            if 0 <= ty < self.H:
                grat[ty, max(icx - 1, 0):icx + 2] = True
        self.grat = grat & self.face

    def decay(self, dt, tau_f, tau_g):
        self.flash *= math.exp(-dt / tau_f)
        self.glow *= math.exp(-dt / tau_g)

    def trace(self, px, py, rate, power, tau_f, tau_g):
        """Sweep the beam through the points. Every sample gets the same beam
        time, spread along its path, so slow parts of the trace glow brightest."""
        if len(px) == 0:
            return
        if self.last_pt is not None:  # carry on from where the last frame ended
            px = np.concatenate([[self.last_pt[0]], px])
            py = np.concatenate([[self.last_pt[1]], py])
        self.last_pt = (px[-1], py[-1])
        if len(px) < 2:
            px, py = np.repeat(px, 2), np.repeat(py, 2)
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
        energy = (1.0 / rate) / steps[seg]
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


# ----------------------------------------------------------------- main ----


def main():
    ap = argparse.ArgumentParser(description="1960s military CRT audio scope for the terminal")
    ap.add_argument("--rate", type=int, default=192000,
                    help="capture sample rate (default 192000)")
    ap.add_argument("--mic", action="store_true", help="use the microphone instead")
    ap.add_argument("--demo", action="store_true", help="draw a test tone, no audio needed")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--phosphor", type=int, default=1, choices=range(1, len(PHOSPHORS) + 1),
                    help="1=P7 radar 2=P1 green 3=P12 orange 4=P31 green-short")
    ap.add_argument("--square", action="store_true", help="square screen instead of round tube")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)  # for testing
    args = ap.parse_args()

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
    show_info = True
    peak = 0.05

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
                elif k == "h":
                    show_info = not show_info
                    size = None

            cols, rows = shutil.get_terminal_size((100, 30))
            plot_rows = rows - 1 if show_info else rows
            if (cols, plot_rows) != size:
                size = (cols, plot_rows)
                screen = Screen(cols, plot_rows, round_tube)
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
            scale = (0.92 / peak if auto else 1.0) * gain * screen.R
            screen.decay(dt, tau_f, tau_g)
            screen.trace(screen.cx + x * scale, screen.cy - y * scale,
                         src.rate, power, tau_f, tau_g)

            lines = screen.render(palette, show_grat)
            buf = ["\x1b[H"]
            for r, line in enumerate(lines):
                buf.append("\x1b[%d;1H%s" % (r + 1, line))
            if show_info:
                info = (" CRT-SCOPE │ %s │ %s │ gain %s%.1fx │ glow %.1fs │ beam %d │ %dkHz │ %dfps"
                        "   p m [ ] i I + - a g o h q"
                        % (PHOSPHORS[ph][0].strip(), "GONIO" if goniometer else "XY",
                           "auto·" if auto else "", (0.92 / peak if auto else 1) * gain,
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
