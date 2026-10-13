"""A reaction time test for the drawn keyboard's screen. The keyboard's own runs in its firmware and reports nothing,
so this is our own: wait for green, then press any key. Times are in seconds; no Qt, so it tests on its own."""
import random

TOO_FAST = 0.1                                  # nobody answers faster than this: it was a guess
WAIT = (1.5, 4.5)                               # how long the screen stays red, at random


class Reaction:
    def __init__(self, rng=random):
        self.rng = rng
        self.state = 'ready'                    # ready, wait (red), go (green), done (a time), early (too soon)
        self.ms = self.best = None
        self.go_at = 0.0                        # when green comes (or came)

    def press(self, now):
        if self.state == 'wait':
            self.state = 'early'
        elif self.state == 'go':
            self.ms = (now - self.go_at) * 1000
            self.state = 'done' if self.ms >= TOO_FAST * 1000 else 'early'
            if self.state == 'done':
                self.best = self.ms if self.best is None else min(self.best, self.ms)
        else:                                   # ready, or a result on screen: start again
            self.state, self.go_at = 'wait', now + self.rng.uniform(*WAIT)

    def step(self, now):
        if self.state == 'wait' and now >= self.go_at:
            self.state, self.go_at = 'go', now
