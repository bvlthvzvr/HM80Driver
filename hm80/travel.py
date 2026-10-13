"""Smooth but faithful playback of the keyboard's live key travel.

The keyboard reports the key that moved last in every heartbeat, about 33 times a second. Drawing each report the
moment it arrives makes the depth jump in 30 ms steps, and a quick tap is only two or three reports long. So the
reports are played back a little late instead, with the depth interpolated in a straight line between them. The depth
then passes exactly through every reported value and never overshoots, and it moves smoothly at the display's frame
rate. This module has no Qt in it, so it can be tested on its own.
"""
import math
from collections import deque

PERIOD = 0.030      # seconds between heartbeats
DELAY = 0.045       # playback runs this far behind the newest report, so it always has a next one to move toward


class TravelTrack:
    def __init__(self, period=PERIOD, delay=DELAY):
        self.period, self.delay = period, delay
        self.samples = deque(maxlen=64)         # (time, key, triggered, depth, deepest), oldest first
        self.key = None                         # the last key seen: it is the one shown back at rest
        self._at = -math.inf                    # playback time of the frame drawn last

    def clear(self):
        self.samples.clear()
        self.key, self._at = None, -math.inf

    def push(self, now, reports):
        """Take the reports of one read, oldest first: (key, triggered, depth, deepest), or None for "no key moving".
        A read holds more than one heartbeat when the program was busy, so those are spread back over the time before
        `now` rather than all landing on the same instant."""
        for i, report in enumerate(reports):
            self._add(now - (len(reports) - 1 - i) * self.period, report)

    def _add(self, t, report):
        if report is None:                      # at rest: the last key, up and not triggered
            if self.key is None:
                return
            report = (self.key, False, 0, 0)
        report = tuple(report)
        self.key = report[0]
        if self.samples:
            last = self.samples[-1]
            if last[1:] == report:              # nothing changed, so there is nothing to add
                return
            t = max(t, last[0] + 1e-3)          # times only move forward
            if t - last[0] > 1.5 * self.period: # it sat still for a while: stay put until just before the change,
                self.samples.append((t - self.period, *last[1:]))   # rather than drifting toward it all that time
        self.samples.append((t, *report))

    def at(self, now):
        """(key, triggered, depth, deepest) to show at `now`, or None before any key has been seen."""
        return self._value(now - self.delay)

    def frame(self, now):
        """`at`, for drawing a run of frames: a frame that would step over a reported value shows that value instead,
        so no report (the peak of a tap, above all) falls between two frames."""
        t = now - self.delay
        vertex = next((s[0] for s in self.samples if self._at < s[0] <= t), None)
        self._at = t if vertex is None else vertex
        return self._value(self._at)

    def _value(self, t):
        samples = self.samples
        if not samples:
            return None
        j = len(samples) - 1
        while j > 0 and samples[j][0] > t:      # the newest report that playback has already reached
            j -= 1
        t0, key, on, depth, deepest = samples[j]
        if j + 1 < len(samples) and t > t0 and samples[j + 1][1] == key:    # a different key is a cut, not a sweep
            t1, _, _, depth1, deepest1 = samples[j + 1]
            u = (t - t0) / (t1 - t0)
            depth, deepest = depth + (depth1 - depth) * u, deepest + (deepest1 - deepest) * u
        return key, on, depth, deepest

    def active(self, now):
        """True while frames are still owed: playback has not yet shown the newest report."""
        return bool(self.samples) and self.samples[-1][0] > min(now - self.delay, self._at)
