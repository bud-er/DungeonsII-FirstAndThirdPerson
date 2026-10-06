"""Choose body-facing intent while the selected camera stays independent.

Ordinary movement faces its camera-relative travel direction before a later
native forward roll commits its target data. This pure policy neither delays
input nor guarantees native tick ordering for a simultaneous move/dodge press.
Native ranged-facing handoffs are applied by the caller before this policy.
Third-person dodge requests briefly outrank attack facing before commitment;
they do not change whether a native ability may start.
"""
import math


class DodgeFacingIntent:
    """A brief pre-commit bearing from a fresh physical dodge press.

    A held button at startup or after cancellation must be released before a
    request is accepted. This state never synthesizes or delays native input.
    """
    WINDOW = .15

    def __init__(self, button_mask):
        self.button_mask = button_mask
        self.clear()

    def clear(self):
        self.previous = self.last_time = self.started = self.bearing = None

    def sample(self, buttons, yaw, axes, now, *, eligible):
        # Validate even an ineligible sample before changing any state.
        direction = control_yaw(yaw, axes, eligible=True, movement_locked=False,
                                attack_active=False, roll_active=False, idle_follows_view=False)
        if not math.isfinite(now):
            raise RuntimeError('Invalid dodge facing clock')
        buttons &= self.button_mask
        if not eligible or (self.last_time is not None and now < self.last_time):
            self.clear()
            return
        previous = self.previous
        self.previous, self.last_time = buttons, now
        if previous is not None and buttons & ~previous:
            # Every fresh request replaces the last one. A neutral second
            # press must not inherit travel from an earlier dodge request.
            self.started = now if direction is not None else None
            self.bearing = direction

    def pending(self, now):
        if not math.isfinite(now):
            raise RuntimeError('Invalid dodge facing clock')
        if self.last_time is not None and now < self.last_time:
            self.clear()
            return None
        self.last_time = now
        if self.started is None or now-self.started >= self.WINDOW:
            self.started = self.bearing = None
        return self.bearing


def control_yaw(yaw, axes, *, eligible, movement_locked, attack_active, roll_active,
                idle_follows_view=True, dodge_yaw=None):
    """Return the intended controller yaw, or None to retain native control.

    ``axes`` are the already-deadzoned, normalized movement input shared with
    KeyboardMovement. A pending dodge may face travel before an attack, but
    never overrides a committed roll or a native movement lock. Without that
    request, explicit attacks retain their existing camera-facing behavior.
    """
    x,y=axes
    if not all(math.isfinite(value) for value in (yaw,x,y)) or max(abs(x),abs(y))>1 or \
            (dodge_yaw is not None and not math.isfinite(dodge_yaw)):
        raise RuntimeError('Invalid locomotion facing input')
    if not eligible or roll_active:
        return None
    view=(yaw+180)%360-180
    if dodge_yaw is not None and not movement_locked:
        return (dodge_yaw+180)%360-180
    if attack_active:
        return view
    if movement_locked:
        return None
    if math.hypot(x,y)<=.1:
        return view if idle_follows_view else None
    return (view+math.degrees(math.atan2(x,y))+180)%360-180
