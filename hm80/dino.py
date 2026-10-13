"""A small Chrome-style dino game for the drawn keyboard's screen. The keyboard's own dino runs in its firmware and
reports nothing, so this is our own: a key press jumps. Lengths are in screen heights; no Qt, so it tests on its own."""
import random

WIDTH = 16 / 9                                  # the screen is 16:9
GROUND, X, W, H = 0.85, 0.2, 0.22, 0.3          # ground line, the dino's left edge, its size
JUMP, GRAVITY = 2.2, 5.5                        # take-off speed and pull (heights per second, and per second squared)
START, TOP, ACCEL = 1.25, 2.4, 0.04              # scroll speed at the start, its limit, and how fast it grows
WALK, MIN_X, MAX_X = 1.3, 0.02, 1.1             # walking speed, and how far back and forward the dino can go


class Dino:
    def __init__(self, rng=random):
        self.rng = rng
        self.held = {-1: False, 1: False}       # the walking keys (back, forward) that are down
        self.reset()

    def reset(self):
        self.x = X                              # left edge
        self.y = self.vy = 0.0                  # height above the ground, and its rate of change
        self.cacti = []                         # [left edge, width, height], oldest first
        self.speed, self.dist, self.since = START, 0.0, 0.0
        self.state, self.gap = 'ready', 0.0     # ready (waiting for a key), run, over

    def _wake(self):
        if self.state == 'over' and self.since > 0.4:   # not so soon that the press that crashed also restarts
            self.reset()
        if self.state == 'ready':
            self.state = 'run'

    def jump(self):
        self._wake()
        if self.state == 'run' and self.y == 0:
            self.vy = JUMP

    def walk(self, direction, down):
        """A walking key (-1 back, 1 forward) went down or up."""
        if down:
            self._wake()
            self.held[-direction] = False       # the newest press wins, so a release that was never reported can't stick
        self.held[direction] = down

    def step(self, dt):
        if self.state == 'over':
            self.since += dt
        if self.state != 'run':
            return
        self.vy -= GRAVITY * dt
        self.y = max(0.0, self.y + self.vy * dt)
        if self.y == 0:
            self.vy = 0.0
        self.x = min(MAX_X, max(MIN_X, self.x + (self.held[1] - self.held[-1]) * WALK * dt))
        dx = self.speed * dt
        self.dist += dx
        self.speed = min(TOP, self.speed + ACCEL * dt)
        for c in self.cacti:
            c[0] -= dx
        self.cacti = [c for c in self.cacti if c[0] + c[1] > 0]
        if not self.cacti or WIDTH - self.cacti[-1][0] > self.gap:      # room for the next one
            self.cacti.append([WIDTH, self.rng.choice((0.06, 0.09, 0.12)), self.rng.uniform(0.14, 0.22)])
            self.gap = 0.8 * self.speed + 0.5 + self.rng.random() * 0.8   # always enough to land and jump again
        if any(c[0] < self.x + W - 0.02 and c[0] + c[1] > self.x + 0.02 and self.y < c[2] for c in self.cacti):
            self.state = 'over'
