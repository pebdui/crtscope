# crtscope

A 1960s military cathode ray tube in your terminal, driven by whatever your
speakers are playing.

The left channel moves the beam sideways and the right channel moves it up and
down, so stereo sound draws Lissajous figures. The beam leaves phosphor glow
behind that fades over time, and it burns brighter where it moves slowly, just
like a real tube. Round glass face, bezel, etched graticule, dimmer edges.

## Trace an image

Give it a picture and the beam draws its outlines instead of the Lissajous
pattern, like oscilloscope music that draws pictures. The music is fed into the
same deflection as the picture, so the lines wobble and ripple with the sound,
bass hits make the picture pulse bigger, and loud parts make the beam burn
brighter. In quiet moments it settles back to a dim, steady picture.

```sh
python3 crtscope.py logo.png
```

Logos, line art, icons and high-contrast pictures work best; busy photos turn
into a lot of little squiggles. Transparent PNGs use their shape's edge.
`+` / `-` set how strongly the music bends the lines.

## Phosphors

| # | Phosphor | Look |
| --- | --- | --- |
| 1 | P7 radar (default) | blue-white flash, long yellow-green afterglow |
| 2 | P1 green | classic scope green, medium glow |
| 3 | P12 orange | orange radar glow |
| 4 | P31 green-short | crisp green, almost no trail |

## Run it

Needs Python 3, numpy, and `parec` (PulseAudio or PipeWire) to hear what your
speakers are playing. Use a terminal with truecolor support.

```sh
python3 crtscope.py               # listen to system audio at 192kHz
python3 crtscope.py --demo        # 3:2 test tone, no audio needed
python3 crtscope.py cat.png       # trace an image instead
python3 crtscope.py --mic         # listen to the microphone instead
python3 crtscope.py --phosphor 2 --square --fps 144
```

Simple tones, synth pads and "oscilloscope music" look best. Busy mixes turn
into a glowing ball of noise, as they would on a real scope.

## Keys

| Key | Does |
| --- | --- |
| `p` | next phosphor |
| `m` | XY / goniometer (mono shows as a vertical line) |
| `[` / `]` | shorter / longer afterglow |
| `i` / `I` | dimmer / brighter beam |
| `+` / `-` | gain (how much the music bends the lines, with an image) |
| `a` | auto gain on/off |
| `g` | graticule on/off |
| `o` | round tube / square screen |
| `<` / `>` | squash / stretch sideways (only needed if your terminal hides its cell size) |
| `h` | hide the info bar |
| `q` / Esc | quit |
