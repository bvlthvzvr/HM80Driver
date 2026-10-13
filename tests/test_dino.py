import random

from hm80.dino import MIN_X, W, X, Dino


def play(g, seconds, bot=False):
    for _ in range(int(seconds / 0.03)):
        if bot and any(0 < c[0] - (X + W) < g.speed * 0.2 for c in g.cacti):   # jump a moment before a cactus
            g.jump()
        g.step(0.03)
        if g.state == 'over':
            break


def test_dino():
    g = Dino(random.Random(1))
    g.step(1)
    assert g.state == 'ready' and g.dist == 0       # nothing moves until a key is pressed
    g.jump()
    g.step(0.1)
    assert g.state == 'run' and g.y > 0
    play(g, 1)
    assert g.y == 0                                 # it lands again
    g = Dino(random.Random(1))
    g.walk(1, True)                                 # walking forward starts the game too
    g.step(0.5)
    assert g.state == 'run' and g.x > X
    g.walk(-1, True)                                # forward was never released, but the newer press wins
    play(g, 5, bot=True)
    assert g.x == MIN_X                             # it stops at the back edge
    g = Dino(random.Random(1))
    g.jump()
    play(g, 60)
    assert g.state == 'over'                        # never jumping hits a cactus
    g.jump()
    assert g.state == 'over'                        # too soon after the crash to restart
    g.step(0.5)
    g.jump()
    assert g.state == 'run' and g.cacti == [] and g.dist == 0
    for seed in range(5):
        g = Dino(random.Random(seed))
        g.jump()
        play(g, 90, bot=True)
        assert g.state == 'run'                     # a jump at the right time always clears them
