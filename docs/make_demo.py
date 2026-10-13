"""Render docs/demo.webp, the picture at the top of the README: the keyboard as the app draws it, with its backlight
off, a moving picture on its mini screen and a rainbow running along its light bar.

    PYTHONPATH=. python docs/make_demo.py [--gif FILE ...] [--out docs/demo.webp] [--width 960]

Needs ffmpeg with libwebp. With --gif the screen plays those GIFs one after another, each for at least 2 s (a short one
repeats), and the light bar is timed to repeat with the whole loop. Without it, a small animation drawn here is used."""
import argparse
import math
import os
import subprocess
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QImageReader, QLinearGradient, QPainter, QPixmap, QRadialGradient,
                           QRegion)
from PySide6.QtWidgets import QApplication, QWidget

from hm80.board import KeyboardView
from hm80.gui import mm
from hm80.protocol import Profile

FPS = 25


class StillClock:
    """The lighting reads the clock, so a clock that stands still keeps the board a still picture."""
    def elapsed(self):
        return 0

    def start(self):
        pass


def aurora(t):
    """The default screen picture: three soft lights drifting over a dark gradient. t in 0..1, and 1 is 0 again."""
    img = QImage(240, 135, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bg = QLinearGradient(0, 0, 240, 135)
    bg.setColorAt(0, QColor('#0a0f1f'))
    bg.setColorAt(1, QColor('#0b2a30'))
    p.fillRect(img.rect(), bg)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    a = 2 * math.pi * t
    p.setPen(Qt.PenStyle.NoPen)
    for color, cx, cy, r, phase, kx, ky in (('#00b894', 120, 70, 95, 0.0, 1, 2), ('#8e5bd6', 120, 60, 85, 2.1, 2, 1),
                                            ('#3f8cff', 120, 75, 80, 4.2, 1, 1)):
        c = QPointF(cx + 70 * math.sin(a * kx + phase), cy + 32 * math.cos(a * ky + phase))
        g = QRadialGradient(c, r)
        g.setColorAt(0, QColor(color))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(g)
        p.drawEllipse(c, r, r)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    font = QFont()
    font.setPixelSize(40)
    font.setBold(True)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
    p.setFont(font)
    p.setPen(QColor(255, 255, 255, 235))
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, 'HM80')
    p.end()
    return QPixmap.fromImage(img)


def gif_frames(path):
    """(end time in ms, picture) for each frame of a GIF."""
    reader, frames, t = QImageReader(path), [], 0
    while reader.canRead():
        delay = reader.nextImageDelay() or 100
        img = reader.read()
        if img.isNull():
            break
        t += delay
        frames.append((t, QPixmap.fromImage(img)))
    if not frames:
        raise SystemExit(f'Cannot read {path}')
    return frames


def playlist(paths):
    """(end ms, frames, length) for each GIF in turn, each repeated until it has played for 2 s."""
    out, total = [], 0
    for path in paths:
        frames = gif_frames(path)
        length = frames[-1][0]
        total += length * max(1, math.ceil(2000 / length))
        out.append((total, frames, length))
    return out


def picture(plays, ms):
    """What the screen shows ms into the loop."""
    start = 0
    for end, frames, length in plays:
        if ms < end:
            return next(pm for e, pm in frames if e > (ms - start) % length)
        start = end


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--gif', nargs='+', help='play these GIFs on the keyboard screen, one after another')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo.webp'))
    ap.add_argument('--width', type=int, default=960)
    args = ap.parse_args()
    app = QApplication([])
    plays = playlist(args.gif) if args.gif else None
    seconds = plays[-1][0] / 1000 if plays else 4.0
    frames, cycles = max(2, round(seconds * FPS)), max(1, round(seconds / 2))    # the light bar: a cycle per ~2 s
    profile = Profile.factory()
    board = KeyboardView()
    board._clock = StillClock()
    board.values = {i: mm(profile.get(0, i, 'init') + profile.get(0, i, 'press')) for i in board.keys}
    board.set_lighting(bytes([0x50, 0xAA, 0xFF, 0xC0, 0x00]))     # keys: off, light bar: Cycle Right
    board.resize(args.width, board.heightForWidth(args.width))
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(frames):
            t = i / frames
            board._phase = cycles * t                             # where the light bar's rainbow has got to
            board.lcd = picture(plays, t * seconds * 1000) if plays else aurora(t)
            pm = QPixmap(board.size())
            pm.fill(Qt.GlobalColor.transparent)                   # no background, so it suits a light or a dark page
            board.render(pm, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
            pm.save(os.path.join(tmp, f'{i:04d}.png'))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(FPS), '-i', os.path.join(tmp, '%04d.png'),
                        '-c:v', 'libwebp_anim', '-lossless', '0', '-q:v', '80', '-loop', '0', args.out], check=True)
    print(f'{args.out}: {frames} frames, {seconds:.1f} s, {os.path.getsize(args.out) / 1e6:.1f} MB')


if __name__ == '__main__':
    main()
