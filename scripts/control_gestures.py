"""Mutually exclusive click/hold gestures; no keys or game memory are written."""
import math


class ClickGesture:
    def __init__(self, *, maximum=3, window=.30, max_tap=.22, hold=2.):
        if maximum not in (2, 3) or not 0 < max_tap <= window < hold:
            raise ValueError('Invalid gesture timing')
        self.maximum, self.window, self.max_tap, self.hold = maximum, window, max_tap, hold
        self.last_time = None
        self.cancel()

    def cancel(self):
        self.blocked, self.down = True, False
        self.started, self.released = None, None
        self.count = 0

    def update(self, down, now, *, eligible=True, consumed=False):
        if not math.isfinite(now):
            raise ValueError('Invalid gesture clock')
        rollback = self.last_time is not None and now < self.last_time
        self.last_time = now
        if not eligible or consumed or rollback:
            self.cancel()
            return None
        down = bool(down)
        if self.blocked:
            if not down:
                self.blocked = False
            return None
        if down:
            if not self.down:
                if self.released is not None and now-self.released > self.window:
                    # A new press after a pause cannot complete an old multi-click.
                    self.count = 0
                self.started, self.down = now, True
            elif self.started is not None and now-self.started >= self.hold:
                self.cancel()
                return 'hold'
            return None
        if self.down:
            self.down = False
            if self.started is not None and now-self.started <= self.max_tap:
                self.count += 1
                self.released = now
                if self.count == self.maximum:
                    result = self.count
                    self.count, self.started, self.released = 0, None, None
                    return result
            else:
                self.count, self.released = 0, None
            self.started = None
        if self.count and self.released is not None and now-self.released >= self.window:
            result = self.count
            self.count, self.released = 0, None
            return result
        return None


class DirectionDoubleTap:
    """Two deliberate pushes in the same direction, separated by neutral."""
    def __init__(self, window=.36):
        self.window = window
        self.last_time = None
        self.cancel()

    def cancel(self):
        self.armed = False
        self.direction = self.started = None

    def update(self, axes, now, eligible=True):
        x, y = axes
        if not all(math.isfinite(v) for v in (x, y, now)) or math.hypot(x, y) > 1.000001:
            raise ValueError('Invalid sprint direction')
        rollback = self.last_time is not None and now < self.last_time
        self.last_time = now
        if not eligible or rollback:
            self.cancel()
            return False
        strength = math.hypot(x, y)
        if strength < .2:
            self.armed = True
            return False
        if strength < .75 or not self.armed:
            return False
        self.armed = False
        current = (x/strength, y/strength)
        trigger = (self.started is not None and now-self.started <= self.window and
                   sum(a*b for a, b in zip(current, self.direction)) >= .85)
        self.direction, self.started = current, None if trigger else now
        return trigger


class SprintIntent:
    def __init__(self):
        self.directions = DirectionDoubleTap()
        self.last_time = None
        self.cancel()

    def cancel(self):
        self.active, self.pending_until = False, None
        self.directions.cancel()

    def update(self, axes, now, *, request=False, eligible=True, blocked=False, double_tap=True):
        if not math.isfinite(now):
            raise ValueError('Invalid sprint clock')
        rollback = self.last_time is not None and now < self.last_time
        self.last_time = now
        if rollback:
            self.cancel()
            eligible = False
        gesture = self.directions.update(axes, now, eligible=eligible and not blocked and double_tap)
        if not eligible or blocked:
            self.cancel()
            return False
        if request or gesture:
            # A deliberate second sprint tap stops sprint; a neutral double
            # flick can only start it because neutral already cancels it.
            if request and self.active:
                self.active, self.pending_until = False, None
                return False
            self.pending_until = now+.8
        moving = math.hypot(*axes) >= .2
        if not moving:
            self.active = False
        elif self.pending_until is not None and now <= self.pending_until:
            self.active, self.pending_until = True, None
        if self.pending_until is not None and now > self.pending_until:
            self.pending_until = None
        return self.active
