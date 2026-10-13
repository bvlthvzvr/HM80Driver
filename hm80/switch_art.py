"""The switch on the Key Depth panel: a 3D render of a Hall-effect switch, cut open to show its spring, made to move.

switch_base.png is the render without its stem post and without the coils of its spring, and switch_post.png is the post
alone. Both are the render's own pixels, cropped to the switch: their top left corner is ORIGIN in the render's
coordinates, which every number in this file uses. A press slides the post down into the housing and squeezes the spring,
which is drawn over the peg it winds round, in front of the housing's solid parts."""
import math
import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform)

HERE = os.path.dirname(__file__)
ORIGIN = (55, 28)                       # where the images' top left corner is, in the render
SIZE = (801, 644)                       # the images' size
MID_X, FOOT_Y = 455.0, 660.0            # the switch's center line, and the bottom of its base
SCALE = 0.21                            # panel pixels per render pixel, up and down
ASPECT = 0.82                           # and across, as a share of that: the render is a three-quarter view, which
                                        # makes the switch look wider than it is
TRAVEL = 120.0                          # how far the post goes down in a full press, in render pixels
POST_X = (375, 525)                     # the post's columns, and the row where it goes into the housing
POST_BASE = 202
HALO = (80, 18, -6, 3)                  # the glow: layers, how far out and in its edge reaches (panel pixels at SCALE),
                                        # and each layer's opacity
POST_TOP = 5                            # the row of the images where the post begins

# The spring is a helix round a pale peg, seen from a little above: each coil shows as a half ellipse in front of the
# peg. These are measured from the render: the height of each coil's lowest point, and the helix's shape.
CENTER_X, RADIUS, DEPTH, SLOPE, BOTTOM_OFFSET = 450.0, 110.0, 36.9, 10.26, 53.0
TOP_COILS = (275, 285, 292)             # closed coils against the stem: they go down with it
SPRING_COILS = (308, 352, 397, 441, 486, 530)  # the ones that squeeze
FLOOR_COILS = (548, 553, 562, 570)      # closed coils on the floor: they stay
# where the spring can be seen: through the tower's window, and the open front of the base below it
VISIBLE = ((447, 262), (531, 262), (531, 418), (566, 418), (566, 590), (336, 590), (336, 438), (398, 463), (437, 463),
           (436, 432))
WIRE = ((QColor(30, 30, 32), 8.4, 0.0), (QColor(72, 72, 76), 4.6, 0.0), (QColor(150, 150, 155, 170), 1.5, -1.3))


def coil(low):
    """One coil's visible half (the near side of the helix) as a path; `low` is the height of its lowest point."""
    y0 = low - BOTTOM_OFFSET
    n = 36
    pts = [QPointF(CENTER_X + RADIUS * math.cos(math.pi * i / n),
                   y0 + SLOPE * math.pi * i / n + DEPTH * math.sin(math.pi * i / n)) for i in range(n + 1)]
    path = QPainterPath(pts[0])
    for pt in pts[1:]:
        path.lineTo(pt)
    return path


class SwitchArt:
    def __init__(self):
        self.images = {}                # (device pixel ratio, scale) -> (housing, post), scaled to the panel
        self.coils = {}                 # a coil's lowest point at rest -> its path
        self.visible = QPainterPath()
        self.visible.addPolygon(QPolygonF([QPointF(x, y) for x, y in VISIBLE]))
        self.visible.closeSubpath()

    def pixmaps(self, dpr, k):
        if (dpr, k) not in self.images:
            size = QSize(round(SIZE[0] * k * ASPECT * dpr), round(SIZE[1] * k * dpr))
            made = []
            for name in ('switch_base.png', 'switch_post.png'):
                pix = QPixmap(os.path.join(HERE, name)).scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                               Qt.TransformationMode.SmoothTransformation)
                pix.setDevicePixelRatio(dpr)
                made.append(pix)
            self.images[(dpr, k)] = tuple(made)
        return self.images[(dpr, k)]

    @staticmethod
    def spring(sink):
        """Where each coil's lowest point is when the post is `sink` down: the top coils go with it, the floor ones stay,
        and the rest are spread evenly between."""
        top, floor = TOP_COILS[-1], FLOOR_COILS[0]
        return ([y + sink for y in TOP_COILS]
                + [y + sink * (floor - y) / (floor - top) for y in SPRING_COILS] + list(FLOOR_COILS))

    def paint(self, p, cx, base, dpr, fraction, k=SCALE, glow=None):
        """Draw the switch standing on `base`, centered on `cx`, the post down by `fraction` (0 to 1) of its travel, with a
        soft glow of the color `glow` round the whole post if there is one. Returns how far the post went down,
        in panel pixels."""
        housing, post = self.pixmaps(dpr, k)
        sink = TRAVEL * max(0.0, min(fraction, 1.0))
        snap = lambda v: round(v * dpr) / dpr                       # images go on device pixels, so they stay sharp
        kx = k * ASPECT
        left, top = snap(cx + (ORIGIN[0] - MID_X) * kx), snap(base + (ORIGIN[1] - FOOT_Y) * k)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(QPointF(left, top), housing)

        enters = snap(top + (POST_BASE - ORIGIN[1]) * k)            # the post is cut off where it goes into the housing
        p.save()
        p.setClipRect(QRectF(0, 0, 100000, enters))
        p.drawPixmap(QPointF(left, snap(top + sink * k)), post)
        if sink > 0:                                                # and it is darker where it goes in
            x0, x1 = left + (POST_X[0] - ORIGIN[0]) * kx, left + (POST_X[1] - ORIGIN[0]) * kx
            shade = QLinearGradient(0, enters - 14 * k, 0, enters)
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, int(80 * min(sink / 30, 1))))
            p.fillRect(QRectF(x0, enters - 14 * k, x1 - x0, 14 * k), shade)
        p.restore()
        if glow:                                                    # a soft halo round the whole post, going down with it
            x0, x1 = left + (POST_X[0] - ORIGIN[0]) * kx, left + (POST_X[1] - ORIGIN[0]) * kx
            y0 = top + (POST_TOP + sink) * k
            p.save()
            p.setClipRect(QRectF(0, 0, 100000, enters))             # like the post, it stops where it goes in
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(glow.red(), glow.green(), glow.blue(), HALO[3]))
            w = x1 - x0
            for j in range(HALO[0]):                                # a stack of rectangles from wide to narrow: the
                x = 2 * j / (HALO[0] - 1) - 1                       # edge fades out over a long, smooth slope, because
                m = (HALO[1] + HALO[2] + (HALO[1] - HALO[2]) * -x ** 3) / 2 * k / SCALE   # most of the steps bunch in the middle
                r = max(0.0, w / 2 + m)
                p.drawRoundedRect(QRectF(x0 - m, y0 - m, w + 2 * m, enters - y0 + m + 40), r, r)
            p.restore()

        p.setTransform(QTransform().translate(cx, base).scale(kx, k).translate(-MID_X, -FOOT_Y), True)
        p.setClipPath(self.visible)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for low, now in zip(TOP_COILS + SPRING_COILS + FLOOR_COILS, self.spring(sink)):
            if low not in self.coils:
                self.coils[low] = coil(low)
            path = self.coils[low].translated(0, now - low)
            for color, width, lift in WIRE:
                p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPath(path.translated(0, lift) if lift else path)
        p.restore()
        return sink * k
