# Run: pytest. The drawn keyboard's model of the per-key backlight effects.
from hm80.board import backlight
from hm80.protocol import Profile


def test_backlight_effects():
    assert backlight(2, 10, 255, 200, 100, 0, 50, 20, 0.3, [], 0) == (10, 255, 200)          # static
    assert backlight(0, 10, 255, 200, 100, 0, 50, 20, 0.3, [], 0) is None                   # off
    left, right = (backlight(5, 0, 255, 255, 100, 0, x, 30, 0.5, [], 0)[0] for x in (10, 110))
    assert right - left == 100                                       # cycle left/right: hue runs along the board
    hits = [(47, 40, 40, 1000)]                                      # A pressed at 1 s; speed 255 = 1 unit a ms
    assert backlight(12, 0, 255, 255, 255, 47, 40, 40, 0, hits, 1000)[2] == 255  # reactive simple: A lights
    assert backlight(12, 0, 255, 255, 255, 48, 50, 40, 0, hits, 1000) is None    # S doesn't
    assert backlight(12, 0, 255, 255, 255, 47, 40, 40, 0, hits, 1300) is None    # and A has faded
    assert backlight(15, 0, 255, 255, 255, 0, 200, 40, 0, hits, 1010) is None    # splash: the ring isn't there yet
    assert backlight(15, 0, 255, 255, 255, 0, 200, 40, 0, hits, 1170)[2] > 0     # now it is
    assert backlight(14, 0, 255, 255, 255, 0, 80, 70, 0, hits, 1060) is None     # nexus: off its cross


def test_custom_lighting_layout():
    mode, mask, frames = Profile.factory().custom_lighting(0)
    assert len(frames) == 16 and all(len(f) == 330 for f in frames)
