import random

from hm80.reaction import Reaction


def test_reaction():
    r = Reaction(random.Random(1))
    r.step(10)
    assert r.state == 'ready'                       # nothing happens until a key is pressed
    r.press(0)
    assert r.state == 'wait'
    r.step(0.5)
    assert r.state == 'wait'                        # still red
    r.press(0.6)
    assert r.state == 'early'                       # too soon
    r.press(1)
    r.step(10)
    assert r.state == 'go' and r.go_at == 10
    r.press(10.25)
    assert r.state == 'done' and abs(r.ms - 250) < 1e-6 and abs(r.best - 250) < 1e-6
    r.press(20)
    r.step(30)
    r.press(30.2)
    assert abs(r.ms - 200) < 1e-6 and abs(r.best - 200) < 1e-6      # the best time is kept
    r.press(40)
    r.step(50)
    r.press(50.05)
    assert r.state == 'early' and abs(r.best - 200) < 1e-6          # 50 ms was a guess, so it does not count
