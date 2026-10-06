"""One camera toggle per deliberate two-second left-stick click hold."""
import math


LEFT_THUMB = 0x0040


class CameraHold:
    def __init__(self, duration=2.):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Hold duration must be positive')
        self.duration = duration
        self.started = None
        self.blocked = True  # A held button at startup must first be released.

    def cancel(self):
        self.started = None
        self.blocked = True

    def update(self, buttons, now, eligible=True):
        if not math.isfinite(now):
            raise ValueError('Invalid clock value')
        if not buttons & LEFT_THUMB:
            self.started, self.blocked = None, False
            return False
        if not eligible:
            self.cancel()
            return False
        if self.blocked:
            return False
        if self.started is None:
            self.started = now
        elif now < self.started:
            self.cancel()
        elif now-self.started >= self.duration:
            self.cancel()
            return True
        return False
