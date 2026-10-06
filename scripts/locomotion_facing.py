"""Choose body-facing intent while the first-person camera stays independent.

Ordinary movement faces its camera-relative travel direction before a later
native forward roll commits its target data. This pure policy neither delays
input nor guarantees native tick ordering for a simultaneous move/dodge press.
Native ranged-facing handoffs are applied by the caller before this policy.
"""
import math


def control_yaw(yaw, axes, *, eligible, movement_locked, attack_active, roll_active):
    """Return the intended controller yaw, or None to retain native control.

    ``axes`` are the already-deadzoned, normalized movement input shared with
    KeyboardMovement. Roll commitment outranks attacks; explicit attacks face
    the view even when their own ability has locked movement. Other movement
    locks retain native control. Neutral movement returns to the current view.
    """
    x,y=axes
    if not all(math.isfinite(value) for value in (yaw,x,y)) or max(abs(x),abs(y))>1:
        raise RuntimeError('Invalid locomotion facing input')
    if not eligible or roll_active:
        return None
    view=(yaw+180)%360-180
    if attack_active:
        return view
    if movement_locked:
        return None
    if math.hypot(x,y)<=.1:
        return view
    return (view+math.degrees(math.atan2(x,y))+180)%360-180
