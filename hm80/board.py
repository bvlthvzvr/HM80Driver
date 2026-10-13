"""The HM80 drawn like the real board: click a key to select it, drag to paint a selection."""
import math
from collections import deque

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QMovie, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import dino, reaction
from .protocol import LAYOUT
from .switch_art import SCALE as SWITCH_SCALE, SwitchArt
from .travel import DELAY

PAGE_REACTIVE, PAGE_USER, PAGE_REACTION, PAGE_DINO = 1, 2, 3, 4   # the keyboard screen's pages, numbered as it reports them
GAMES = {PAGE_REACTION, PAGE_DINO}              # our own versions of the keyboard's two games, played on the drawn screen
PAGE_DRAWN = {PAGE_REACTIVE, *GAMES}
WALK_KEYS = {81: -1, 83: 1}                     # in the dino game the left and right arrows walk; any other key jumps
TRACE_MS = 2500                                 # how much key travel the reactive page's trace shows
FN_GAP = 0.15                                   # the function row (and screen) sits apart from the rest
WIDTH, HEIGHT, PAD = 17.3, 6 + FN_GAP, 0.35     # key area and case margin, in key units
SHIFTED = dict(zip("`1234567890-=[]\\;',./", '~!@#$%^&*()_+{}|:"<>?'))
CASE, CASE_EDGE = QColor('#e6dcc0'), QColor('#cdc2a2')
CREAM, GRAY, DARK_GRAY = QColor('#efe5c9'), QColor('#a39e93'), QColor('#6e726f')
MODE_TAG = QColor('#1d7f75')                    # the tag on a key that has a key mode
RED, SELECT, BAR_OFF = QColor('#e0442e'), QColor('#2f7cf6'), QColor('#a7a6a2')
REMAP_ON_DARK = QColor('#ffd479')               # legend of a key that does something else, on a dark cap (else plain ink)
# HM80 colorway (retro): cream case and alphas, warm gray modifiers / F5-F8 / nav, dark gray Enter and space, red Esc.
COLORS = {0: RED, 58: DARK_GRAY, 75: DARK_GRAY, 76: DARK_GRAY, 77: DARK_GRAY,
           **dict.fromkeys((5, 6, 7, 8, 13, 27, 28, 29, 30, 44, 45, 46, 59, 70, 71, 72, 73, 74, 78, 79, 80, 81, 82, 83),
                           GRAY)}
SCREEN, LIGHT_BAR, LOGO = (QRectF(15.3, 0, 2, 1), QRectF(15.35, 3.43 + FN_GAP, 1.9, 0.14),
                           QRectF(16.42, 4.1 + FN_GAP, 0.8, 0.8))
BADGE_PLATE = QColor('#d8d8d5')                 # the badge: matte silver plastic with a diamond logo engraved in it
# The logo, traced from the real badge on the badge's own 0..1 square (x right, y down): the diamond, then each engraved
# line as (width in badge widths, points). Round caps and joins soften the corners at this size.
BADGE_DIAMOND = ((0.50, 0.05), (0.94, 0.52), (0.49, 0.96), (0.06, 0.48))
BADGE_LINES = (
    (0.012, ((0.485, 0.125), (0.14, 0.457), (0.20, 0.462))), (0.012, ((0.535, 0.125), (0.865, 0.468))),
    (0.014, ((0.12, 0.50), (0.205, 0.50))), (0.014, ((0.25, 0.665), (0.47, 0.67), (0.50, 0.63))),
    (0.014, ((0.40, 0.695), (0.475, 0.79))), (0.014, ((0.585, 0.715), (0.515, 0.785))),
    (0.014, ((0.735, 0.495), (0.745, 0.545), (0.78, 0.55), (0.795, 0.525), (0.87, 0.525))),
    (0.024, ((0.41, 0.285), (0.50, 0.235), (0.595, 0.30))),
    (0.024, ((0.36, 0.318), (0.69, 0.326))),
    (0.024, ((0.31, 0.378), (0.64, 0.385), (0.675, 0.415), (0.645, 0.452), (0.48, 0.458))),
    (0.02, ((0.40, 0.425), (0.47, 0.425), (0.47, 0.46))), (0.02, ((0.375, 0.575), (0.465, 0.445))),
    (0.024, ((0.485, 0.525), (0.625, 0.53), (0.655, 0.56), (0.62, 0.59), (0.545, 0.585))),
    (0.024, ((0.54, 0.668), (0.64, 0.668), (0.70, 0.625), (0.715, 0.59))),
    (0.014, ((0.245, 0.462), (0.30, 0.575))), (0.014, ((0.27, 0.43), (0.315, 0.415))),
)


BLOOM_REACH = (0.17, 0.135, 0.105, 0.075, 0.05)  # how far past the tube each layer of the LED bloom reaches (key units)
BLOOM_ALPHA = (4, 7, 11, 17, 26)                # and how strongly it is drawn (0..255); a slight bloom


CYCLE_SPAN = 150                                # degrees of hue along the strip in the cycle and pendulum effects
PENDULUM_SWING = 0.5                            # how far the pendulum's rainbow swings each way, in strip lengths
GRADIENT_STOPS = 48                             # color stops along the strip, so blends between them are smooth
# speed byte -> effect cycles per second is a guess; time one cycle on the real strip to calibrate it.
SLOWEST, FASTEST = 0.1, 1.3


GROOVE_OUTER, GROOVE_INNER = 0.17, 0.09         # how far the key-area groove's outer and inner edges are from the keys
CAP_INSET, FACE_ROUND = 0.012, 0.07             # a cap's gap to its cell edge, and how round its top face's corners are
CAP_OUTLINE = QColor(30, 26, 22, 225)           # the dark line round a cap, where it meets the plate

GLOW_RES, GLOW_REACH, GLOW_ALPHA = 4, 0.12, 60  # a lit key's LED spilling past its cap: drawn at 4 px a key unit and
                                                 # scaled up smoothly, which blurs it; reach in key units; alpha 0..255
# and light leaking out from under the cap, hugging its outline in layers that fade outward. LEAK_REACH is how far each
# layer reaches past the cap (key units) and LEAK_ALPHA how strongly it is drawn (0..255). They are drawn small, at
# LEAK_RES px a key unit, and scaled up smoothly like the glow, which is far cheaper than drawing them full size.
LEAK_RES, LEAK_REACH, LEAK_ALPHA = 24, (0.085, 0.06, 0.04, 0.02), (30, 55, 90, 140)
HITS = 8                                         # key presses the reactive effects remember


def backlight(effect, hue, sat, val, speed, key, x, y, cycles, hits, now):
    """(hue 0..255, saturation, value) of one key's LED, or None when it is dark. The firmware's effects carry QMK's
    RGB-matrix names, so this follows QMK's math; x, y are on QMK's 224 x 64 grid, cycles is how many effect cycles
    have run, hits are recent presses (key, x, y, ms) and now is the time in ms. Custom lighting is drawn elsewhere."""
    # an approximation of the firmware's effects; tune against the real keyboard if one looks off.
    t, dx, dy = cycles * 256, x - 112, y - 32
    dist, angle = math.hypot(dx, dy), math.atan2(dy, dx) / (2 * math.pi) * 256
    if effect in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        if effect == 3:                                            # breathe
            val *= 0.5 - 0.5 * math.cos(cycles * 2 * math.pi)
        a = cycles * 2 * math.pi
        hue += {2: 0, 3: 0, 4: t, 5: x - t, 6: y - t, 7: 1.5 * dist + t, 8: abs(dy) + x - t, 9: angle + t,
                10: dist - t - angle, 11: dy * math.cos(a) + dx * math.sin(a)}[effect]
        return hue, sat, val
    if effect < 12:
        return None
    lit = shift = 0.0                                              # reactive: dark until keys are pressed
    for k, hx, hy, ms in hits:
        tick = (now - ms) * (speed + 1) / 256                     # how far this press's effect has run
        if effect == 12:                                           # simple: the pressed key lights and fades
            lit = max(lit, 255 - tick) if k == key else lit
            continue
        hdx, hdy = x - hx, y - hy
        d = math.hypot(hdx, hdy)
        e = tick + d * 5 if effect == 13 else tick - d             # wide: a fading patch; nexus, splash: a ring
        if e < 0 or effect == 14 and (d > 72 or abs(hdx) > 8 and abs(hdy) > 8):
            continue                                               # not reached yet, or off nexus's cross
        e = min(e, 255)
        lit += 255 - e
        shift = hdy / 4 if effect == 14 else shift + e if effect == 15 else 0
    return (hue + shift, sat, val * min(lit, 255) / 255) if lit > 0 else None


def white(alpha):
    return QColor(255, 255, 255, alpha)


def black(alpha):
    return QColor(0, 0, 0, alpha)


def ramp(a, b, *stops):
    """Gradient from point a to point b through (position, color) stops."""
    g = QLinearGradient(a, b)
    for at, color in stops:
        g.setColorAt(at, color)
    return g


def key_rects():
    rects = {}
    for y, row in enumerate(LAYOUT):
        x = 0
        for idx, label, w, gap in row:
            x += gap
            rects[idx] = (QRectF(x, y + (FN_GAP if y else 0), w, 1), label)
            x += w
    return rects


class KeyboardView(QWidget):
    selectionChanged = Signal()
    screenClicked = Signal()

    def __init__(self):
        super().__init__()
        self.keys = key_rects()
        self._pos = {i: (r.center().x() / WIDTH * 224, r.center().y() / HEIGHT * 64)   # on QMK's 224 x 64 grid
                     for i, (r, _) in self.keys.items()}
        self.selected, self.values = set(), {}     # values: key index -> small text shown on the cap
        self.legends = {}                           # key index -> what it does instead of its legend ('' = nothing)
        self.tags = {}                              # key index -> short names of all its modes, shown in a corner
        self.marks = {}                             # key index -> QColor washed over its cap (calibration results)
        self.lcd = None                             # QPixmap shown on the screen module
        self.page = None                            # which page the screen is on (None: show the saved image)
        self.reactive = None                        # (key, depth 0..1, depth in mm)
        self.dino, self.reaction, self._game_at = dino.Dino(), reaction.Reaction(), 0   # played while on their pages
        self._game_tick = QTimer(self, interval=33, timeout=self._game_step)
        self.trace = deque()                        # (clock ms, depth 0..1) of the reactive page's scrolling line
        self._trace_tick = QTimer(self, interval=33, timeout=self._trace_step)
        self.bar = None                             # light bar: (effect, hue, sat, brightness, speed)
        self.backlight = None                       # keys: (effect, hue, sat, brightness, speed)
        self.overlay, self.custom = False, None     # custom per-key lighting: shown over the effect? (mode, mask, frames)
        self._shadow_img = self._shadow_stamp = None  # the caps' shadows, and the size they were drawn for
        self._caps, self._cap_stamp = {}, None      # resting caps drawn ahead: (key, selected) -> (pixmap, where)
        self.hits = []                              # recent key presses for the reactive effects: (key, x, y, ms)
        self._lit = {}                              # key index -> (LED color, strength 0..1) while painting
        self.live = None                            # (key, depth 0..1, triggered) of the key being pressed
        self._movie = None
        self._adding = None                         # while dragging: True adds keys, False removes them
        self._clock = QElapsedTimer()
        self._clock.start()
        self._phase, self._phase_at = 0.0, 0        # light-bar effect cycles run so far, and when (clock ms)
        self._ticks = 0                             # animation timer ticks so far
        self._tick = QTimer(self, interval=33, timeout=self._animate)
        self.setMinimumSize(640, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return int(w * (HEIGHT + 2 * PAD) / (WIDTH + 2 * PAD))

    def sizeHint(self):
        return QSize(980, self.heightForWidth(980))

    # ---------- geometry ----------
    def _scale(self):
        s = min(self.width() / (WIDTH + 2 * PAD), self.height() / (HEIGHT + 2 * PAD))
        return s, (self.width() - WIDTH * s) / 2, (self.height() - HEIGHT * s) / 2

    def _px(self, r):
        s, ox, oy = self._scale()
        return QRectF(ox + r.x() * s, oy + r.y() * s, r.width() * s, r.height() * s)

    def key_at(self, pos):
        return next((i for i, (r, _) in self.keys.items() if self._px(r).contains(pos)), None)

    # ---------- what the keyboard is showing ----------
    def show_screen(self, path):
        """Play the image or GIF the keyboard's screen holds (the last one uploaded)."""
        if self._movie:
            self._movie.stop()
            self._movie = None
        self.lcd = None
        if path and path.lower().endswith('.gif'):
            self._movie = QMovie(path, b'', self)
            self._movie.frameChanged.connect(self._lcd_frame)
            self._movie.start()
        elif path:
            self.lcd = QPixmap(path)
        self.update()

    def set_page(self, page):
        if page in GAMES and page != self.page:     # a fresh game each time the screen comes round to it
            self.dino, self.reaction = dino.Dino(), reaction.Reaction()
            self._game_at = self._clock.elapsed()
            self._game_tick.start()
        elif page not in GAMES:
            self._game_tick.stop()
        self.page = page
        self.update(self._px(SCREEN).toAlignedRect())

    def set_reactive(self, *state):
        self.reactive = state
        if state[1] > 0:                            # a key is down: the line runs until it has all scrolled away
            self.trace.append((self._clock.elapsed(), state[1]))
            self._trace_tick.start()
        self._redraw_page()

    def _game_step(self):
        now = self._clock.elapsed()
        if self.page == PAGE_DINO:
            self.dino.step(min((now - self._game_at) / 1000, 0.1))
        else:
            self.reaction.step(now / 1000)
        self._game_at = now
        self.update(self._px(SCREEN).toAlignedRect())

    def _trace_step(self):
        now = self._clock.elapsed()
        self.trace.append((now, self.reactive[1] if self.reactive else 0))
        while self.trace[0][0] < now - TRACE_MS:
            self.trace.popleft()
        if not any(d for _, d in self.trace):
            self.trace.clear()
            self._trace_tick.stop()
        self._redraw_page()

    def _redraw_page(self):
        if self.page == PAGE_REACTIVE:
            self.update(self._px(SCREEN).toAlignedRect())

    def _lcd_frame(self):
        self.lcd = self._movie.currentPixmap()
        self.update(self._px(SCREEN).toAlignedRect())

    def set_lighting(self, s, on=True, custom=None):
        """Simulate the backlight and light bar from a layer's 0x16 struct (the keyboard can't report what they show).
        custom: that layer's custom per-key lighting from the profile, (mode, frame mask, frames)."""
        self._advance()                             # run up to now at the old speed, so a new speed doesn't jump
        effect = s[0] & 0x0F if on else 0
        self.bar = ((s[0] >> 4) & 7 if on else 0, s[1], s[2], s[3], s[4])
        self.backlight, self.overlay, self.custom = (effect, s[1], s[2], s[3], s[4]), on and s[0] >= 0x80, custom
        if self.bar[0] in (2, 3, 4, 5, 6) or effect >= 3 or len(self._frames()) > 1:
            self._tick.start()
        else:
            self._tick.stop()
        self.update()

    def _frames(self):
        """The custom-lighting frames that play on this layer, if it shows custom lighting at all."""
        if not self.custom or not (self.backlight[0] == 1 or self.overlay):
            return []
        mode, mask, _ = self.custom                 # 0x10 plays the ticked frames in order, 0x11 at random, n shows n
        if mode in (0x10, 0x11):
            return [f for f in range(16) if mask >> f & 1] or [0]
        return [mode if mode < 16 else 0]

    def _moving(self):
        """True while the backlight changes by itself, so the whole board needs repainting."""
        effect, speed = self.backlight[0], self.backlight[4]
        if effect >= 12:                            # reactive: until the last press has died away
            return bool(self.hits) and self._clock.elapsed() - self.hits[-1][3] < 520 * 256 / (speed + 1)
        return effect >= 3 or len(self._frames()) > 1

    def _animate(self):
        # a whole-board repaint costs ~13 ms, so steady effects redraw it every other tick (15 fps) and
        # reactive ones every tick; cache the case and keys' static parts in a pixmap if that is still too much.
        self._ticks += 1
        if self.backlight and self._moving() and (self.backlight[0] >= 12 or self._ticks % 2):
            return self.update()
        self._animate_bar()

    def _animate_bar(self):
        m = self._scale()[0] * (max(BLOOM_REACH) + 0.02) + 2        # the bloom reaches this far past the bar
        self.update(self._px(LIGHT_BAR).adjusted(-m, -m, m, m).toAlignedRect())

    def set_live(self, live):
        """Sink and light up the key that is being pressed right now."""
        g = self._scale()[0] * (GLOW_REACH + 0.2) + 2
        for k in {self.live and self.live[0], live and live[0]} - {None}:
            self.update(self._px(self.keys[k][0]).adjusted(-g, -g, g, g).toAlignedRect())
        key = live[0] if live else self.live and self.live[0]
        if self.page == PAGE_DINO and key in WALK_KEYS:    # the walking keys count while they are down, and a release
            self.dino.walk(WALK_KEYS[key], bool(live and live[2]))      # shows as a report that is not triggered
        if live and live[2] and not (self.live and self.live[0] == live[0] and self.live[2]):
            self.hits = (self.hits + [(live[0], *self._pos[live[0]], self._clock.elapsed())])[-HITS:]  # pressed
            if self.page == PAGE_DINO and live[0] not in WALK_KEYS:
                self.dino.jump()
            elif self.page == PAGE_REACTION:            # the press was reported a moment before it was played back
                self.reaction.press(self._clock.elapsed() / 1000 - DELAY)
        self.live = live
        if self.bar and self.bar[0] == 7:               # the light bar's key depth effect follows the key
            self._animate_bar()

    def _advance(self):
        """Move the light-bar effect on by the time since it last moved, at the current speed."""
        now = self._clock.elapsed()
        if self.bar:
            self._phase += (now - self._phase_at) / 1000 * (SLOWEST + self.bar[4] / 255 * (FASTEST - SLOWEST))
        self._phase_at = now

    def _bar_color(self, x, n):
        """Color at position x (0..1) along the bar, n effect cycles in. Timing is an approximation."""
        effect, hue, sat, val, _ = self.bar
        hue = hue * 360 / 256
        if effect == 2:                                            # breathe
            val *= 0.5 - 0.5 * math.cos(n * 2 * math.pi)
        elif effect == 3:                                          # cycle through colors
            hue += n * 360
        elif effect in (4, 5):                                     # rainbow moving left / right
            hue, sat = hue + (x + (1 if effect == 4 else -1) * n) * CYCLE_SPAN, 255
        elif effect == 6:                                          # gravity pendulum: the rainbow swings to and fro
            hue, sat = hue + (x + PENDULUM_SWING * math.sin(n * 2 * math.pi)) * CYCLE_SPAN, 255
        elif effect == 7:                                          # key depth: filled as far as the key is pressed
            fill, edge = (self.live[1] if self.live else 0.0), 0.06
            val *= max(0.0, min(1.0, (fill * (1 + edge) - x) / edge))
            if self.live and self.live[2]:                         # past its actuation point, until it is reset: the
                hue += 180                                         # opposite color (a fixed one could match the layer's)
        # LEDs behind a diffuser: blend the unlit silver toward the LED color. An LED looks bright even at a modest
        # setting, so the brightness curve is steep at the low end; a lit color is also lifted a little toward white.
        led, k = QColor.fromHsv(int(hue) % 360, sat, 255), (max(0.0, min(val, 255)) / 255) ** 0.35
        mixed = (o + (l - o) * k for o, l in zip(BAR_OFF.getRgb()[:3], led.getRgb()[:3]))
        return QColor(*(int(c + (255 - c) * 0.14 * k) for c in mixed))

    # ---------- selection ----------
    def set_selection(self, keys):
        self.selected = set(keys)
        self.update()
        self.selectionChanged.emit()

    def _toggle(self, idx):
        self.selected ^= {idx}
        self.update()
        self.selectionChanged.emit()

    def mousePressEvent(self, e):
        if self._px(SCREEN).contains(e.position()):
            return self.screenClicked.emit()
        idx = self.key_at(e.position())
        if idx is not None:
            self._adding = idx not in self.selected
            self._toggle(idx)

    def mouseMoveEvent(self, e):
        idx = self.key_at(e.position())
        if self._adding is not None and idx is not None and (idx in self.selected) != self._adding:
            self._toggle(idx)

    def mouseReleaseEvent(self, _):
        self._adding = None

    # ---------- painting ----------
    def _font(self, s, size, bold=False):
        f = QFont()
        f.setPixelSize(max(7, int(s * size)))
        f.setBold(bold)
        f.setStyleStrategy(QFont.StyleStrategy.NoSubpixelAntialias)   # no color fringes on small legends
        return f

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s, ox, oy = self._scale()
        p.setPen(QPen(CASE_EDGE, max(1.0, s * 0.03)))
        p.setBrush(CASE)
        p.drawRoundedRect(QRectF(ox - PAD * s, oy - PAD * s, (WIDTH + 2 * PAD) * s, (HEIGHT + 2 * PAD) * s),
                          s * 0.3, s * 0.3)
        self._recess(p, s, ox, oy)
        self._screen(p, s)
        self._light_bar(p, s)
        self._badge(p, s)
        p.drawImage(0, 0, self._shadows(s))
        self._lit = self._leds()
        if self._lit:                                                 # each lit key's LED, spilling round its cap
            glow = QImage(int((WIDTH + 2) * GLOW_RES), int((HEIGHT + 2) * GLOW_RES),
                          QImage.Format.Format_ARGB32_Premultiplied)
            glow.fill(Qt.GlobalColor.transparent)
            gp = QPainter(glow)
            for idx, (c, k) in self._lit.items():
                r = self.keys[idx][0].adjusted(1 - GLOW_REACH, 1 - GLOW_REACH, 1 + GLOW_REACH, 1 + GLOW_REACH)
                gp.fillRect(QRectF(r.x() * GLOW_RES, r.y() * GLOW_RES, r.width() * GLOW_RES, r.height() * GLOW_RES),
                            QColor(c.red(), c.green(), c.blue(), int(GLOW_ALPHA * k)))
            gp.end()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QRectF(ox - s, oy - s, (WIDTH + 2) * s, (HEIGHT + 2) * s), glow)
            leak = QImage(int((WIDTH + 2) * LEAK_RES), int((HEIGHT + 2) * LEAK_RES),
                          QImage.Format.Format_ARGB32_Premultiplied)
            leak.fill(Qt.GlobalColor.transparent)
            lp = QPainter(leak)                                 # the light comes from under the cap: it shows only as
            lp.setRenderHint(QPainter.RenderHint.Antialiasing)  # a glow hugging the cap's outline, fading outward
            lp.setPen(Qt.PenStyle.NoPen)
            for grow, alpha in zip(LEAK_REACH, LEAK_ALPHA):
                for idx, (c, k) in self._lit.items():
                    cell, inset = self.keys[idx][0], CAP_INSET - grow  # the cap, its cell inset by CAP_INSET, covers this
                    box = cell.adjusted(inset, inset, -inset, -inset).translated(1, 1)    # 1: the image's margin
                    lp.setBrush(QColor(c.red(), c.green(), c.blue(), int(alpha * k)))
                    lp.drawRoundedRect(QRectF(box.x() * LEAK_RES, box.y() * LEAK_RES,
                                              box.width() * LEAK_RES, box.height() * LEAK_RES),
                                       (0.09 + grow) * LEAK_RES, (0.09 + grow) * LEAK_RES)
            lp.end()
            p.drawImage(QRectF(ox - s, oy - s, (WIDTH + 2) * s, (HEIGHT + 2) * s), leak)
        for idx, (r, label) in self.keys.items():
            self._key(p, idx, self._px(r), label, s)

    def _leds(self):
        """Color and strength (0..1) of every lit key's LED right now."""
        if not self.backlight:
            return {}
        self._advance()
        effect, hue, sat, val, speed = self.backlight
        frames, now, out = self._frames(), self._clock.elapsed(), {}
        if frames:                                  # 4 frames a cycle; random is a fixed shuffle
            n = int(self._phase * 4)
            frame = self.custom[2][frames[(n * 7 if self.custom[0] == 0x11 else n) % len(frames)]]
        for idx, (x, y) in self._pos.items():
            rgb = frames and frame[3 * idx:3 * idx + 3]
            if rgb and any(rgb):                    # custom lighting (overlaid on the effect, or as the effect)
                c, k = QColor(*rgb), val / 255
            elif hsv := backlight(effect, hue, sat, val, speed, idx, x, y, self._phase, self.hits, now):
                c, k = QColor.fromHsv(int(hsv[0] * 360 / 256) % 360, int(hsv[1]), 255), hsv[2] / 255
            else:
                continue
            if k > 0.02:
                out[idx] = (c, k ** 0.5)            # an LED looks bright even when dim
        return out

    def _recess(self, p, s, ox, oy):
        """The groove round the whole key area: the plate the keys, light bar and logo sit on lies a little below the
        rim of the case. The wall of the drop is in shadow at the top and left, and catches the light at the bottom
        and right, like the other openings."""
        def outline(grow, radius):                    # the key area grown by `grow` key units, as a rounded rectangle
            path = QPainterPath()
            path.addRoundedRect(QRectF(ox - grow * s, oy - grow * s, (WIDTH + 2 * grow) * s, (HEIGHT + 2 * grow) * s),
                                radius * s, radius * s)
            return path
        rim, plate = outline(GROOVE_OUTER, GROOVE_OUTER + 0.03), outline(GROOVE_INNER, GROOVE_INNER + 0.03)
        box, line = rim.boundingRect(), max(1.0, s * 0.015)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ramp(box.topLeft(), box.bottomRight(),
                        (0, CASE.darker(138)), (0.45, CASE.darker(114)), (1, CASE.lighter(105))))
        p.drawPath(rim.subtracted(plate))             # the sloping wall of the drop
        p.setBrush(CASE.darker(103))
        p.drawPath(plate)                             # the plate, a shade lower than the rim
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(ramp(box.topLeft(), box.bottomRight(), (0, white(0)), (0.5, white(80)), (1, white(190)))),
                      line))
        p.drawPath(rim)                               # the lip of the rim catching the light
        p.setPen(QPen(QBrush(ramp(box.topLeft(), box.bottomRight(), (0, black(120)), (1, black(55)))), line))
        p.drawPath(plate)                             # the foot of the wall, where it meets the plate

    def _badge(self, p, s):
        """The badge: a matte silver plastic plate set into the case, its top and left edges catching the light, with
        the logo engraved in it (each line a dark groove with a light lip on the side away from the light)."""
        plate, none = self._px(LOGO), Qt.PenStyle.NoPen
        radius, line = plate.width() * 0.07, max(1.0, s * 0.015)
        p.setPen(none)
        p.setBrush(black(70))                                                      # the thin gap round the plate
        p.drawRoundedRect(plate.adjusted(-line, -line, line, line), radius + line, radius + line)
        p.setBrush(BADGE_PLATE)
        p.drawRoundedRect(plate, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(ramp(plate.topLeft(), plate.bottomRight(), (0, white(150)), (0.5, white(0)), (0.55, black(0)),
                                  (1, black(70)))), line))
        p.drawRoundedRect(plate.adjusted(line / 2, line / 2, -line / 2, -line / 2), radius, radius)
        w = plate.width()
        point = lambda q: QPointF(plate.x() + q[0] * w, plate.y() + q[1] * w)
        paths = []
        for width, points in ((0.028, BADGE_DIAMOND + BADGE_DIAMOND[:1]), *BADGE_LINES):
            path = QPainterPath(point(points[0]))
            for q in points[1:]:
                path.lineTo(point(q))
            paths.append((max(0.8, width * w), path))
        lip = max(0.7, w * 0.012)                                                   # light from above: the lip is below
        for dx, dy, color, scale in ((lip * 0.5, lip, white(200), 0.8), (0, -lip * 0.3, QColor(52, 52, 50, 170), 0.75)):
            for width, path in paths:
                pen = QPen(color, width * scale)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawPath(path.translated(dx, dy))

    def _light_bar(self, p, s):
        """The light bar: a round diffuser tube lying in a pocket (a groove) in the case."""
        none, bar, lit = Qt.PenStyle.NoPen, self._px(LIGHT_BAR), bool(self.bar and self.bar[0])
        groove = bar.adjusted(-s * 0.055, -s * 0.055, s * 0.055, s * 0.055)
        down = lambda r, *stops: ramp(r.topLeft(), r.bottomLeft(), *stops)         # a gradient from top to bottom
        p.setPen(none)                                                             # the groove, shaded at the top
        p.setBrush(down(groove, (0, CASE.darker(155)), (1, CASE.darker(112))))
        p.drawRoundedRect(groove, groove.height() / 2, groove.height() / 2)
        p.setBrush(Qt.BrushStyle.NoBrush)                                          # and lit along its lower lip
        p.setPen(QPen(QBrush(down(groove, (0, black(80)), (0.5, black(0)), (0.6, white(0)), (1, white(190)))),
                      max(1.0, s * 0.02)))
        p.drawRoundedRect(groove, groove.height() / 2, groove.height() / 2)
        p.setPen(none)
        if lit:                                         # lit: the colors run along the tube
            self._advance()
            colors = [self._bar_color(i / GRADIENT_STOPS, self._phase) for i in range(GRADIENT_STOPS + 1)]
            body = QLinearGradient(bar.left(), 0, bar.right(), 0)
            for i, c in enumerate(colors):
                body.setColorAt(i / GRADIENT_STOPS, c)
            # a very slight bloom just past the tube: each part blooms by how far it differs from the unlit silver
            shine = [min(1.0, sum(abs(a - b) for a, b in zip(c.getRgb()[:3], BAR_OFF.getRgb()[:3])) / 220)
                     for c in colors]
            for grow, alpha in zip(BLOOM_REACH, BLOOM_ALPHA):
                g = QLinearGradient(bar.left(), 0, bar.right(), 0)
                for i, (c, k) in enumerate(zip(colors, shine)):
                    g.setColorAt(i / GRADIENT_STOPS, QColor(c.red(), c.green(), c.blue(), int(alpha * k)))
                p.setBrush(g)
                r = bar.adjusted(-s * grow, -s * grow, s * grow, s * grow)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        else:
            body = QBrush(BAR_OFF)
        tube = QPainterPath()
        tube.addRoundedRect(bar, bar.height() / 2, bar.height() / 2)
        p.setBrush(body)
        p.drawPath(tube)
        p.save()
        p.setClipPath(tube)
        if lit:                                         # a lit diffuser: an even bright core, barely shaded at the rim
            p.setBrush(down(bar, (0, black(30)), (0.3, white(0)), (0.5, white(60)), (0.7, white(0)), (1, black(40))))
        else:                                           # an unlit one: matte silver, a little darker at the rim
            p.setBrush(down(bar, (0, black(60)), (0.3, black(0)), (0.7, black(0)), (1, black(80))))
        p.drawRect(bar)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(black(95), max(1.0, s * 0.012)))
        p.drawPath(tube)

    def _page(self, p, lcd):
        """Look-alikes of the keyboard's own pages. The reactive one is white with blue type: the key being
        pressed on the left, its depth in mm on the right, and a line scrolling across the bottom while a key
        moves, drawn from the depth over the last couple of seconds."""
        w, h = lcd.width(), lcd.height()
        blue, font = QColor('#0b57d0'), QFont()
        p.fillRect(lcd, Qt.GlobalColor.white)
        p.setPen(blue)
        if self.page == PAGE_DINO:
            return self._dino(p, lcd)
        if self.page == PAGE_REACTION:
            return self._reaction(p, lcd)
        key, depth, mm = self.reactive or ('', 0, 0)
        x0, x1 = lcd.left() + w * 0.05, lcd.right() - w * 0.05
        font.setBold(True)
        font.setPixelSize(int(h * 0.26))
        p.setFont(font)
        row, number = QRectF(x0, lcd.top(), x1 - x0, h * 0.32), f'{mm:.3f}'
        p.drawText(row, Qt.AlignmentFlag.AlignRight, number)
        room = row.width() - QFontMetrics(font).horizontalAdvance(number) - w * 0.04   # the key name gets what is left
        while QFontMetrics(font).horizontalAdvance(key) > room and font.pixelSize() > 6:
            font.setPixelSize(font.pixelSize() - 1)
        p.setFont(font)
        p.drawText(row, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, key)
        if len(self.trace) > 1:
            now, base = self._clock.elapsed(), lcd.top() + h * 0.92
            reach = base - row.bottom() - h * 0.05          # the full depth stops just short of the text above
            line = QPainterPath()
            for i, (t, d) in enumerate(self.trace):
                pt = QPointF(x1 - (now - t) / TRACE_MS * (x1 - x0), base - min(1.0, d) * reach)
                line.moveTo(pt) if not i else line.lineTo(pt)
            p.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), max(1.0, h * 0.02)))   # the system accent
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(line)

    def _reaction(self, p, lcd):
        """Our own reaction time test: red, then green after a random wait; press any key as fast as you can."""
        r, h = self.reaction, lcd.height()
        if r.state == 'done':
            texts = (f'{r.ms:.0f} ms', f'Best {r.best:.0f} ms', 'Press to retry')
        else:
            texts = {'ready': ('Reflex', 'Press any key'), 'wait': ('Wait for green',),
                     'go': ('PRESS!',), 'early': ('Too soon!', 'Press to retry')}[r.state]
        color = {'wait': '#c0392b', 'go': '#2ea043', 'early': '#d29922'}.get(r.state)
        font = QFont()
        font.setBold(True)
        if color:
            p.fillRect(lcd, QColor(color))
        p.setPen(Qt.GlobalColor.white if color else QColor('#0b57d0'))
        font.setPixelSize(int(h * {'go': 0.3, 'done': 0.3, 'ready': 0.3, 'early': 0.26, 'wait': 0.21}[r.state]))
        p.setFont(font)
        p.drawText(lcd.adjusted(0, -h * 0.08, 0, 0), Qt.AlignmentFlag.AlignCenter, texts[0])
        font.setBold(False)
        font.setPixelSize(int(h * 0.14))
        p.setFont(font)
        for i, hint in enumerate(texts[1:]):
            p.drawText(QRectF(lcd.left(), lcd.top() + h * (0.6 + 0.16 * i), lcd.width(), h * 0.16),
                       Qt.AlignmentFlag.AlignCenter, hint)

    def _dino(self, p, lcd):
        """Our own dino game: gray on white like the browser's. The left and right arrows walk, any other key jumps, and a
        press also starts and restarts it."""
        g, h = self.dino, lcd.height()
        gray, none = QColor('#535353'), Qt.PenStyle.NoPen
        gy, left = lcd.top() + dino.GROUND * h, lcd.left()
        p.setPen(QPen(gray, max(1.0, h * 0.012)))
        p.drawLine(QPointF(left, gy), QPointF(lcd.right(), gy))
        for i in range(8):                          # bumps in the ground scroll past at the game's speed
            x = left + ((i * 0.31 - g.dist) % 2.4) * h
            p.drawLine(QPointF(x, gy + h * 0.04), QPointF(x + h * 0.05, gy + h * 0.04))
        p.setPen(none)
        p.setBrush(gray)
        top, dx = gy - (g.y + dino.H) * h, left + g.x * h
        part = lambda fx, fy, fw, fh: QRectF(dx + fx * dino.W * h, top + fy * dino.H * h,
                                              fw * dino.W * h, fh * dino.H * h)
        for r in ((0.45, 0, 0.55, 0.3), (0.2, 0.28, 0.6, 0.45), (0, 0.35, 0.25, 0.18)):   # head, body, tail
            p.drawRect(part(*r))
        run, phase = g.y == 0 and g.state == 'run', int(g.dist * 10) % 2     # running: one foot comes up at a time
        p.drawRect(part(0.25, 0.73, 0.18, 0.12 if run and phase else 0.27))
        p.drawRect(part(0.55, 0.73, 0.18, 0.12 if run and not phase else 0.27))
        p.setBrush(Qt.GlobalColor.white)
        p.drawRect(part(0.75, 0.06, 0.14, 0.1))                             # the eye
        p.setBrush(gray)
        for x, w, ch in g.cacti:
            cx, ctop = left + x * h, gy - ch * h
            for fx, fy, fw, fh in ((0.35, 0, 0.3, 1), (0, 0.3, 0.4, 0.2), (0.6, 0.2, 0.4, 0.2)):   # trunk, two arms
                p.drawRect(QRectF(cx + fx * w * h, ctop + fy * ch * h, fw * w * h, fh * ch * h))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(h * 0.12))
        p.setFont(font)
        p.setPen(gray)
        p.drawText(lcd.adjusted(0, h * 0.03, -lcd.width() * 0.04, 0), Qt.AlignmentFlag.AlignRight,
                   f'{int(g.dist * 10):05d}')
        if g.state != 'run':
            p.drawText(QRectF(lcd.left(), lcd.top() + h * 0.32, lcd.width(), h * 0.15), Qt.AlignmentFlag.AlignCenter,
                       'Press any key' if g.state == 'ready' else 'GAME OVER')

    def _screen(self, p, s):
        """The screen module with depth: a plate sunk into the case, and the glass sunk into the plate. The light
        comes from the top left, so edges facing it are bright and the walls of the openings are shaded."""
        none = Qt.PenStyle.NoPen
        line = max(1.0, s * 0.02)
        pocket = self._px(SCREEN)
        plate = pocket.adjusted(s * 0.05, s * 0.05, -s * 0.05, -s * 0.05)
        # the pocket in the case: shaded under the overhang at the top and left, lit on its far lip
        hole = QPainterPath()
        hole.addRoundedRect(pocket, s * 0.12, s * 0.12)
        p.setPen(none)
        p.setBrush(ramp(pocket.topLeft(), pocket.bottomRight(),
                        (0, CASE.darker(128)), (0.5, CASE.darker(108)), (1, CASE.lighter(105))))
        p.drawPath(hole)
        p.save()
        p.setClipPath(hole)
        for grow, alpha in ((0.05, 14), (0.032, 18), (0.014, 26)):         # the plate's soft shadow on the pocket floor
            p.setBrush(QColor(70, 55, 40, alpha))
            p.drawRoundedRect(plate.translated(s * 0.012, s * 0.03).adjusted(-s * grow, -s * grow, s * grow, s * grow),
                              s * 0.1, s * 0.1)
        p.restore()
        # the plate: matte plastic, one flat color with no shine
        p.setPen(QPen(GRAY.darker(130), line))
        p.setBrush(GRAY)
        p.drawRoundedRect(plate, s * 0.09, s * 0.09)
        # the opening for the glass: its walls are in shadow at the top left and catch light at the bottom right
        h = s * 0.66
        lcd = QRectF(plate.center().x() - h * 8 / 9, plate.center().y() - h / 2, h * 16 / 9, h)   # 240x135 is 16:9
        well = lcd.adjusted(-s * 0.055, -s * 0.055, s * 0.055, s * 0.055)
        p.setPen(none)
        p.setBrush(ramp(well.topLeft(), well.bottomRight(),
                        (0, QColor('#6c6764')), (0.5, GRAY.darker(122)), (1, GRAY.lighter(118))))
        p.drawRoundedRect(well, s * 0.05, s * 0.05)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(ramp(well.topLeft(), well.bottomRight(),
                                  (0, black(110)), (0.5, black(0)), (0.55, white(0)), (1, white(150)))), line))
        p.drawRoundedRect(well, s * 0.05, s * 0.05)
        # the glass: the picture, shadowed by the opening on the top and left, with a faint reflection
        glass = QPainterPath()
        glass.addRoundedRect(lcd, s * 0.02, s * 0.02)
        p.setPen(none)
        p.setBrush(QColor('#1b1b1b'))
        p.drawPath(glass)
        p.save()
        p.setClipPath(glass)
        if self.page in PAGE_DRAWN:
            p.save()                                # it changes the pen, font and brush
            self._page(p, lcd)
            p.restore()
        elif self.lcd and not self.lcd.isNull():
            img = self.lcd.scaled(lcd.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(lcd.center().x() - img.width() / 2), int(lcd.center().y() - img.height() / 2), img)
        p.setBrush(ramp(lcd.topLeft(), QPointF(lcd.left(), lcd.top() + h * 0.2), (0, black(55)), (1, black(0))))
        p.drawRect(lcd)
        p.setBrush(ramp(lcd.topLeft(), QPointF(lcd.left() + h * 0.12, lcd.top()), (0, black(42)), (1, black(0))))
        p.drawRect(lcd)
        p.setBrush(ramp(lcd.bottomRight(), QPointF(lcd.right(), lcd.bottom() - h * 0.1), (0, black(35)), (1, black(0))))
        p.drawRect(lcd)
        p.setBrush(ramp(lcd.topLeft(), lcd.bottomRight(), (0, white(48)), (0.5, white(8)), (1, white(0))))
        p.drawPolygon(QPolygonF([lcd.topLeft(), QPointF(lcd.left() + lcd.width() * 0.62, lcd.top()),
                                 QPointF(lcd.left() + lcd.width() * 0.18, lcd.bottom()), lcd.bottomLeft()]))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(black(170), max(1.0, s * 0.012)))
        p.drawPath(glass)

    def _shadows(self, s):
        """The soft shadows the caps throw on the plate, down and a little to the right, all in one image. They never
        change, so they are drawn once for each size, and lie under every cap so none falls on a neighbor."""
        dpr = self.devicePixelRatioF()
        stamp = (self.width(), self.height(), dpr)
        if self._shadow_stamp != stamp:
            img = QImage(int(self.width() * dpr), int(self.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(Qt.GlobalColor.transparent)
            sp = QPainter(img)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing)
            sp.setPen(Qt.PenStyle.NoPen)
            for cell, _ in self.keys.values():
                r = self._px(cell).adjusted(s * CAP_INSET, s * CAP_INSET, -s * CAP_INSET, -s * CAP_INSET)
                r.translate(s * 0.012, s * 0.045)
                for grow, alpha in ((0.05, 8), (0.03, 12), (0.012, 18)):
                    sp.setBrush(QColor(50, 38, 26, alpha))
                    sp.drawRoundedRect(r.adjusted(-s * grow, -s * grow, s * grow, s * grow),
                                       s * (0.09 + grow), s * (0.09 + grow))
            sp.end()
            self._shadow_img, self._shadow_stamp = img, stamp
        return self._shadow_img

    def _cap_rects(self, cell, ring, s):
        """The rectangles of a cap in its cell: the cap with its dark outline (a ring of color when `ring` is one: the
        key is selected or has a calibration result), the body inside that, and the flat top face inside the body."""
        r = cell.adjusted(s * CAP_INSET, s * CAP_INSET, -s * CAP_INSET, -s * CAP_INSET)
        if ring:
            body = r.adjusted(s * 0.05, s * 0.05, -s * 0.05, -s * 0.05)
        else:                                                         # a thin outline above and left, thicker below
            body = r.adjusted(s * 0.015, s * 0.02, -s * 0.03, -s * 0.04)     # and right, where the light does not reach
        return r, body, body.adjusted(s * 0.10, s * 0.05, -s * 0.17, -s * 0.13)

    def _draw_cap(self, p, cell, color, ring, s, sink=0.0):
        """A cap without its legend, like a real keycap seen from above: a flat top face inside four sloping walls.
        The light is from the top left, so the walls there are lit, nearly the tone of the face, and the right and the
        front ones are in shade, darker toward their foot. Matte: no highlight anywhere. `sink` (0..1) is how far it
        is pressed. Returns the top face."""
        r, body, top = self._cap_rects(cell, ring, s)
        top.translate(0, s * 0.06 * sink)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ring or CAP_OUTLINE)
        p.drawRoundedRect(r, s * 0.05, s * 0.05)
        p.save()
        outer = QPainterPath()
        outer.addRoundedRect(body, s * 0.045, s * 0.045)
        p.setClipPath(outer)
        p.setBrush(color.darker(108))                                # under the walls, so no seam shows between them
        p.drawRect(body)
        across = lambda a, b, *stops: ramp(QPointF(a, 0), QPointF(b, 0), *stops)      # left to right
        down = lambda a, b, *stops: ramp(QPointF(0, a), QPointF(0, b), *stops)       # top to bottom
        tl, tr, bl, br, ftl, ftr, fbl, fbr = (body.topLeft(), body.topRight(), body.bottomLeft(), body.bottomRight(),
                                              top.topLeft(), top.topRight(), top.bottomLeft(), top.bottomRight())
        for corners, brush in (
                ((tl, tr, ftr, ftl), color.darker(107)),                                                  # top wall
                ((tl, ftl, fbl, bl), across(body.left(), top.left(), (0, color.darker(108)), (1, color.darker(102)))),
                ((tr, br, fbr, ftr), across(top.right(), body.right(), (0, color.darker(112)), (1, color.darker(148)))),
                ((bl, fbl, fbr, br), down(top.bottom(), body.bottom(), (0, color.darker(103)), (1, color.darker(124))))):
            p.setBrush(brush)
            p.drawPolygon(QPolygonF(list(corners)))
        p.restore()
        p.setBrush(color.lighter(103))
        p.drawRoundedRect(top, s * FACE_ROUND, s * FACE_ROUND)
        p.setBrush(Qt.BrushStyle.NoBrush)                             # the face's edge, only just there
        p.setPen(QPen(black(26), max(1.0, s * 0.01)))
        p.drawRoundedRect(top, s * FACE_ROUND, s * FACE_ROUND)
        return top

    def _cap(self, idx, cell, color, ring, s):
        """A resting cap (its legend aside) as a pixmap, with where to draw it. It is drawn once and kept, since it only
        changes with its key's ring or the board's size, and drawing every cap each frame is slow."""
        dpr = self.devicePixelRatioF()
        stamp = (self.width(), self.height(), dpr)
        if self._cap_stamp != stamp:
            self._caps, self._cap_stamp = {}, stamp
        stored = (idx, ring and ring.rgba())
        if stored not in self._caps:
            gx, gy = math.floor(cell.x() * dpr), math.floor(cell.y() * dpr)         # the pixmap starts on a device pixel
            origin = QPointF(cell.x() - gx / dpr, cell.y() - gy / dpr)              # and the cap keeps its fraction of one
            pix = QPixmap(math.ceil((origin.x() + cell.width()) * dpr) + 1,
                          math.ceil((origin.y() + cell.height()) * dpr) + 1)
            pix.setDevicePixelRatio(dpr)
            pix.fill(Qt.GlobalColor.transparent)
            cp = QPainter(pix)
            cp.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._draw_cap(cp, QRectF(origin, cell.size()), color, ring, s)
            cp.end()
            self._caps[stored] = (pix, QPointF(gx / dpr, gy / dpr))
        return self._caps[stored]

    def _key(self, p, idx, cell, label, s):
        color, mark, sel = COLORS.get(idx, CREAM), self.marks.get(idx), idx in self.selected
        # a selected key has a blue ring; one with a calibration result has a ring of that result's color, blue or red
        ring = QColor(mark.red(), mark.green(), mark.blue()) if mark else SELECT if sel else None
        live = self.live and self.live[0] == idx
        if live:                                                      # pressed right now: the cap sinks
            top = self._draw_cap(p, cell, color, ring, s, self.live[1])
        else:
            pix, at = self._cap(idx, cell, color, ring, s)
            p.drawPixmap(at, pix)
            top = self._cap_rects(cell, ring, s)[2]
        p.setPen(Qt.PenStyle.NoPen)
        if mark:
            p.setBrush(mark)
            p.drawRoundedRect(top, s * FACE_ROUND, s * FACE_ROUND)
        if live:
            p.setBrush(QColor(46, 184, 92, 110) if self.live[2] else QColor(47, 124, 246, int(30 + 70 * self.live[1])))
            p.drawRoundedRect(top, s * FACE_ROUND, s * FACE_ROUND)
        dark = color in (RED, DARK_GRAY)
        ink = QColor('#f2efe8') if dark else QColor('#2b2a29')
        inner = top.adjusted(s * 0.055, s * 0.03, -s * 0.04, -s * 0.03)
        remap = self.legends.get(idx)
        if remap == '':                                               # does nothing on this layer: faded legend
            ink.setAlpha(70)
        p.setPen(ink)
        if remap:
            big = len(remap) == 1 and remap.isalpha() or remap in '←↑↓→'
            p.setPen(REMAP_ON_DARK if dark else ink)                  # bold; black ink would vanish on a dark cap
            font = self._font(s, 0.25 if big else 0.15, bold=True)
            wide = max(QFontMetrics(font).horizontalAdvance(word) for word in remap.split())
            if wide > inner.width():                                  # shrink a long word to fit the cap
                font.setPixelSize(max(6, int(font.pixelSize() * inner.width() / wide)))
            p.setFont(font)
            p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, remap)
        elif idx in (75, 76, 77):
            pass                                                      # space caps have no legend
        elif label in SHIFTED:                                        # symbol above, like the real caps
            p.setFont(self._font(s, 0.17))
            p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, f'{SHIFTED[label]}\n{label}')
        else:
            big = len(label) == 1 and label.isalpha() or label in '←↑↓→'
            p.setFont(self._font(s, 0.25 if big else 0.15, bold=big))
            p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
        if idx in self.values:
            p.setPen(SELECT.darker(130) if sel else ink if color in (RED, DARK_GRAY) else QColor('#77736a'))
            p.setFont(self._font(s, 0.16))
            p.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, self.values[idx])
        font = self._font(s, 0.12, bold=True)                         # key modes: a small tag each in the top corner,
        for n, tag in enumerate(self.tags.get(idx, ())):              # one below the other when there are several
            pill = QRectF(0, inner.top() + n * s * 0.19, QFontMetrics(font).horizontalAdvance(tag) + s * 0.07, s * 0.17)
            pill.moveRight(inner.right())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(MODE_TAG)
            p.drawRoundedRect(pill, s * 0.05, s * 0.05)
            p.setPen(QColor('white'))
            p.setFont(font)
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, tag)


class SwitchView(QWidget):
    """Live travel of the key being pressed: the switch sinks, with its actuation and reset points marked."""

    def __init__(self):
        super().__init__()
        self.state = None           # (label, depth, triggered, deepest, travel, actuation, reset), all micrometers
        self.tester = (0, 0)        # how far the key moved over its last 10 readings, and where it last released
        self.art = SwitchArt()
        self.setMinimumSize(304, 274)

    def set_state(self, state):
        self.state = state
        self.update()

    def set_tester(self, fluct, release_at):
        if (fluct, release_at) != self.tester:
            self.tester = fluct, release_at
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text, muted = self.palette().color(QPalette.ColorRole.WindowText), QColor('#8a857a')
        font = lambda px, bold=False: (f := QFont(), f.setPixelSize(px), f.setBold(bold))[0]
        if not self.state:
            p.setPen(muted)
            p.setFont(font(13))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Press any key to see\nhow far it travels')
            return
        label, depth, on, deepest, travel, act, reset = self.state
        green, blue, orange, purple = QColor('#2eb85c'), SELECT, QColor('#e0442e'), QColor('#8e5bd6')
        top, bottom = 14, 214                               # the depth gauge: a fixed length, near the top
        cx, base = self.width() - 88, 215                   # center and bottom of the switch drawing
        head = base - 140                                   # just above the top of its post at rest
        half = min(cx, self.width() - cx) - 2
        centered = Qt.AlignmentFlag.AlignCenter
        p.setPen(text)                                      # the key's name, right above the post
        p.setFont(font(18, True))
        name = QFontMetrics(p.font()).elidedText(label, Qt.TextElideMode.ElideRight, int(2 * half))
        p.drawText(QRectF(cx - half, head - 29, 2 * half, 26), centered, name)
        p.setFont(font(26, True))                           # the depth, above the key's name
        p.drawText(QRectF(cx - 90, head - 64, 180, 34), centered, f'{max(depth, 0) / 1000:.2f} mm')
        y = lambda um: top + (bottom - top) * max(0, min(um, travel)) / travel
        # depth gauge: track, filled down to the current depth
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#d9d3c2'))
        p.drawRoundedRect(QRectF(12, top, 24, bottom - top), 6, 6)
        p.setBrush(green if on else blue)
        p.drawRoundedRect(QRectF(12, top, 24, y(depth) - top), 6, 6)
        marks = [(act, orange, f'actuation {act / 1000:.2f}')]
        if on:                                              # rapid trigger: releases once lifted by `reset`
            marks.append((deepest - reset, purple, f'reset {max(deepest - reset, 0) / 1000:.2f}'))
        p.setFont(font(11))
        for um, color, name in marks:
            p.setPen(QPen(color, 2))
            p.drawLine(6, int(y(um)), 42, int(y(um)))
            p.drawText(46, int(y(um)) + 4, name)
        # the switch: a render of it, cut open. The post sinks into the housing as the key goes down and the spring
        # squeezes (see switch_art.py)
        self.art.paint(p, cx, base, self.devicePixelRatioF(), max(0, min(depth, travel)) / travel, SWITCH_SCALE,
                       green if on else None)           # triggered: the tip glows
        # what the keyboard measured, as the official switch tester shows it: how far the key moved lately, the deepest
        # point it reached while triggered and the depth where it last let go (each only while it applies)
        fluct, release_at = self.tester
        cell = (self.width() - 8) / 3
        for i, (name, value) in enumerate((('Fluctuation', fluct), ('Peak', deepest if on else None),
                                           ('Released at', None if on else release_at))):
            box = QRectF(4 + i * cell, base + 10, cell, 44)
            p.setPen(muted)
            p.setFont(font(11))
            p.drawText(QRectF(box.x(), box.y(), cell, 16), centered, name)
            p.setPen(text)
            p.setFont(font(15, True))
            p.drawText(QRectF(box.x(), box.y() + 17, cell, 24), centered,
                       '–' if value is None else f'{max(value, 0) / 1000:.2f} mm')


class GraphView(QWidget):
    """Scrolling graphs of the keyboard's heartbeats, newest on the right, as in the official switch tester: how much
    the key moved lately (it saturates, so it mostly says "the key is moving"), how deep it is (0 to 4.2 mm, the
    official range) with a bar under it while the key is triggered. The curves clear the moment the key is fully up.
    The names and numbers are always there."""
    POINTS = 66                                             # heartbeats on screen, about 2 seconds like the official one
    FLUCT_FULL = 300                                        # micrometers of fluctuation that fill the first graph

    def __init__(self):
        super().__init__()
        self.history = deque(maxlen=self.POINTS)            # (depth, triggered, fluctuation), micrometers
        self.setMinimumHeight(62)                           # fits the smallest window; taller windows give it more
        self.setMaximumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def push(self, series):
        for depth, triggered, fluct in series:
            if depth > 0 or triggered:
                self.history.append((depth, triggered, fluct))
            else:                                           # the key is fully up
                self.history.clear()
        self.update()

    def clear(self):
        self.history.clear()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(13)
        p.setFont(font)
        w, h = self.width(), self.height()
        for name, color, box in (('Fluctuation', QColor('#8e5bd6'), QRectF(0, 0, w * 0.27, h)),
                                 ('Depth', SELECT, QRectF(w * 0.27 + 32, 0, w * 0.73 - 32, h))):
            depth = name == 'Depth'
            p.setPen(QColor('#8a857a'))
            now = self.history[-1][0 if depth else 2] if self.history else 0         # its latest value, beside the name
            p.drawText(box.adjusted(2, 2, 0, 0), f'{name}  {max(now, 0) / 1000:.2f} mm')
            top, bottom = box.top() + 28, box.bottom() - (16 if depth else 4)   # the depth graph leaves room for the bar
            step = box.width() / (self.POINTS - 1)
            line, bar = QPainterPath(), QPainterPath()
            for j, (d, on, fluct) in enumerate(self.history):
                x = box.right() - (len(self.history) - 1 - j) * step
                value, full = (d, 4200) if depth else (fluct, self.FLUCT_FULL)
                y = bottom - (bottom - top) * max(0, min(value, full)) / full
                line.lineTo(x, y) if j else line.moveTo(x, y)
                if depth and on:                            # the bar: under the curve while triggered, broken where not
                    if not j or not self.history[j - 1][1]:
                        bar.moveTo(x - step, box.bottom() - 5)
                    bar.lineTo(x, box.bottom() - 5)
            p.setClipRect(box)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, 2))
            p.drawPath(line)
            p.setPen(QPen(QColor('#2eb85c'), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawPath(bar)
            p.setClipping(False)
