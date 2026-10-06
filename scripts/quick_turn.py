"""Two short L3 clicks produce one eased, additive 180-degree camera turn."""
import math

from camera_transition import smoothstep
from controller_hold import LEFT_THUMB


class QuickTurn:
    def __init__(self, duration=.28/1.15, click_window=.36, max_tap=.22):
        if not all(math.isfinite(v) and v > 0 for v in (duration, click_window, max_tap)) \
                or max_tap > click_window:
            raise ValueError('Invalid quick-turn timing')
        self.duration, self.click_window, self.max_tap = duration, click_window, max_tap
        self.last_time = None
        self.cancel()

    @property
    def active(self):
        return self.started is not None

    def cancel(self):
        self.started, self.applied = None, 0.
        self.tap_started, self.first_tap = None, None
        self.down, self.blocked, self.triggered = False, True, False

    def update(self, buttons, now, eligible=True):
        """Return this frame's yaw delta; triggered marks the accepted second click.

        Cancellation preserves the angle already applied by the caller. A
        release is required after startup, focus/menu loss or controller change.
        Input during the sweep cannot queue another turn.
        """
        if not math.isfinite(now):
            raise ValueError('Invalid quick-turn clock')
        self.triggered = False
        rollback = self.last_time is not None and now < self.last_time
        self.last_time = now
        if not eligible or rollback:
            self.cancel()
            return 0.
        down = bool(buttons & LEFT_THUMB)
        if self.active:
            progress = (now-self.started)/self.duration
            total = 180.*smoothstep(progress)
            delta, self.applied = total-self.applied, total
            self.down, self.blocked = down, down
            if progress >= 1.:
                self.started = None
            return delta
        if self.first_tap is not None and now-self.first_tap > self.click_window:
            self.first_tap = None
        if not down:
            if self.down and self.tap_started is not None:
                if now-self.tap_started <= self.max_tap:
                    self.first_tap = self.tap_started
                else:
                    self.first_tap = None  # A hold is not the first click.
            self.tap_started = None
            self.down, self.blocked = False, False
            return 0.
        if not self.down and not self.blocked:
            if self.first_tap is not None:
                self.started, self.applied = now, 0.
                self.first_tap, self.tap_started = None, None
                self.blocked, self.triggered = True, True
            else:
                self.tap_started = now
        self.down = True
        return 0.
