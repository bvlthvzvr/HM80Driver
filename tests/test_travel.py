# Run: pytest. Smooth playback of the live key travel must stay faithful to what the keyboard reported.
import pytest

from hm80.travel import DELAY, PERIOD, TravelTrack

A, B = 48, 49


def rep(depth, on=True, key=A, deepest=None):
    return key, on, depth, depth if deepest is None else deepest


def tap(depths, start=0.0, key=A):
    """A track fed one heartbeat at a time, like a keyboard that is read on every heartbeat."""
    track = TravelTrack()
    for i, d in enumerate(depths):
        track.push(start + i * PERIOD, [rep(d, d >= 2000, key)])
    return track


def depth_at(track, t):
    return track.at(t)[2]


def play(track, step, until, start=0.0):
    """Every frame a display refreshing every `step` seconds would draw."""
    frames, t = [], start
    while t <= until:
        frames.append(track.frame(t))
        t += step
    return frames


def test_nothing_to_show_until_a_key_has_moved():
    track = TravelTrack()
    assert track.at(1.0) is None and not track.active(1.0)
    track.push(1.0, [None])                                     # at rest and no key seen yet
    assert track.at(1.1) is None
    track.push(1.2, [rep(500)])
    assert track.at(1.2)[0] == A
    track.clear()
    assert track.at(2.0) is None


def test_depth_passes_exactly_through_every_report():
    depths = [0, 900, 2600, 3100, 1200, 0]
    track = tap(depths)
    for i, d in enumerate(depths):
        assert depth_at(track, i * PERIOD + DELAY) == pytest.approx(d)


def test_depth_moves_in_a_straight_line_between_reports():
    track = tap([0, 1000])
    assert depth_at(track, DELAY + PERIOD / 2) == pytest.approx(500)
    assert depth_at(track, DELAY + PERIOD / 4) == pytest.approx(250)


def test_never_overshoots_a_report():
    depths = [0, 900, 2600, 3100, 1200, 0]
    track = tap(depths)
    t = 0.0
    while t < len(depths) * PERIOD + DELAY:
        assert 0 <= depth_at(track, t) <= 3100
        t += 0.001


def test_quick_tap_has_no_big_jumps_between_frames():
    depths = [0, 900, 2600, 3100, 1200, 0]
    track = tap(depths)
    frame = 0.008
    shown = [depth_at(track, i * frame) for i in range(int((len(depths) * PERIOD + DELAY) / frame))]
    biggest_step = max(abs(b - a) for a, b in zip(shown, shown[1:]))
    steepest = max(abs(b - a) for a, b in zip(depths, depths[1:]))      # drawn as it arrives, it would jump this far
    assert biggest_step <= steepest * frame / PERIOD + 1e-6             # no frame moves faster than the real motion did
    assert biggest_step < steepest / 3


def test_triggered_follows_the_report_it_came_with():
    track = tap([1000, 2200, 2400, 1000])
    assert track.at(0 * PERIOD + DELAY + 0.001)[1] is False
    assert track.at(1 * PERIOD + DELAY + 0.001)[1] is True
    assert track.at(3 * PERIOD + DELAY + 0.001)[1] is False


def test_heartbeats_that_arrive_together_are_spread_out():
    track = TravelTrack()
    track.push(1.0, [rep(0, False), rep(1000), rep(2000)])      # a busy moment: three heartbeats in one read
    times = [s[0] for s in track.samples]
    assert times == pytest.approx([1.0 - 2 * PERIOD, 1.0 - PERIOD, 1.0])
    assert [s[3] for s in track.samples] == [0, 1000, 2000]     # none of them is lost


def test_a_key_that_sat_still_does_not_creep_toward_the_next_report():
    track = TravelTrack()
    track.push(0.0, [rep(0, False)])
    track.push(1.0, [rep(1500)])                                # a second later: it was at rest all that time
    assert depth_at(track, 0.9 + DELAY) == pytest.approx(0)
    assert depth_at(track, 1.0 + DELAY) == pytest.approx(1500)


def test_resting_reports_do_not_pile_up_or_keep_the_animation_running():
    track = TravelTrack()
    track.push(0.0, [rep(2400)])
    for i in range(1, 200):
        track.push(i * PERIOD, [None])                          # key released, then 33 idle heartbeats a second
    assert len(track.samples) <= 3
    assert track.at(10.0) == (A, False, 0, 0)                   # still the last key, back at rest
    play(track, 0.008, 10.1, start=10.0)
    assert not track.active(10.1)


def test_active_until_the_newest_report_has_been_shown():
    track = tap([0, 1000, 2000])
    assert track.active(0.0)                                    # nothing drawn yet
    t = 0.0
    while True:
        shown = track.frame(t)
        if not track.active(t):
            break
        t += 0.008
        assert t < 1
    assert shown[2] == pytest.approx(2000)                      # the last frame is the newest report
    assert t >= 2 * PERIOD + DELAY - 0.008
    assert not track.active(t + 1)


def test_every_reported_depth_is_shown_in_some_frame():
    depths = [0, 900, 2600, 3100, 1200, 0]                      # a sharp tap: its 3100 peak is one report
    for step in (0.008, 0.007, 0.0111, 0.016):                  # frame times that never land on a report
        track = tap(depths)
        shown = [f[2] for f in play(track, step, 0.3, start=0.0031)]
        for d in depths:
            assert min(abs(x - d) for x in shown) < 1e-6, (step, d)


def test_frames_stay_close_to_the_pace_of_the_real_motion():
    depths = [0, 900, 2600, 3100, 1200, 0]
    steepest = max(abs(b - a) for a, b in zip(depths, depths[1:]))
    step = 0.008
    shown = [f[2] for f in play(tap(depths), step, 0.3)]
    biggest = max(abs(b - a) for a, b in zip(shown, shown[1:]))
    # a frame pulled back to show a reported value can make the next one cover up to two frames of motion
    assert biggest <= 2 * steepest * step / PERIOD + 1e-6
    assert biggest < steepest / 2                               # still far from drawing each report as it arrives


def test_a_backlog_of_reports_is_caught_up_without_skipping_any():
    track = TravelTrack()
    depths = [0, 800, 2400, 3000, 1500, 0]
    track.push(1.0, [rep(d, d >= 2000) for d in depths])        # the program was blocked, then six heartbeats at once
    shown = [f[2] for f in play(track, 0.008, 1.5, start=1.0)]
    for d in depths:
        assert min(abs(x - d) for x in shown) < 1e-6
    assert not track.active(1.5)


def test_a_different_key_is_a_cut_not_a_sweep():
    track = TravelTrack()
    track.push(0.0, [rep(3000, key=A)])
    track.push(0.1, [rep(200, key=B)])
    mid = track.at(0.05 + DELAY)                                # between the two reports
    assert mid == (A, True, 3000, 3000)                         # A stays as it was, nothing in between is invented
    assert track.at(0.1 + DELAY) == (B, True, 200, 200)


def test_deepest_is_interpolated_too():
    track = TravelTrack()
    track.push(0.0, [rep(1000, deepest=1000)])
    track.push(PERIOD, [rep(1500, deepest=2000)])
    assert track.at(DELAY + PERIOD / 2)[3] == pytest.approx(1500)
