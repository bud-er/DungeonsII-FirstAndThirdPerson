"""Frame-rate-independent easing and rotation interpolation for camera toggles."""
import math


def smoothstep(t):
    t = max(0., min(1., t))
    return t*t*(3-2*t)


def slerp(a, b, t):
    dot = sum(x*y for x, y in zip(a, b))
    if dot < 0:
        b, dot = tuple(-v for v in b), -dot
    dot = max(-1., min(1., dot))
    if dot > .9995:
        result = tuple(x+(y-x)*t for x, y in zip(a, b))
        length = math.sqrt(sum(v*v for v in result))
        return tuple(v/length for v in result)
    angle = math.acos(dot)
    scale = math.sin(angle)
    left, right = math.sin((1-t)*angle)/scale, math.sin(t*angle)/scale
    return tuple(left*x+right*y for x, y in zip(a, b))


def unreal_rotator(q):
    x, y, z, w = q
    pitch = math.asin(max(-1., min(1., 2*(z*x-w*y))))
    yaw = math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))
    roll = -math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))
    return tuple(math.degrees(v) for v in (pitch, yaw, roll))
