"""Integrate look samples without counting a controller time interval twice."""


class LookClock:
    def __init__(self):
        self.previous = None

    def reset(self):
        self.previous = None

    def sample(self, now, mouse, stick, sensitivity, *, discard=False):
        dt = 0. if self.previous is None or discard else max(0., min(.05, now-self.previous))
        self.previous = now
        if discard:
            return 0., 0.
        return (mouse[0]*sensitivity+stick[0]*150*dt,
                -mouse[1]*sensitivity+stick[1]*150*dt)
