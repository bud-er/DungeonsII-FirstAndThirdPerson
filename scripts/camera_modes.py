"""Pure first/shoulder camera geometry and reversible eased transitions."""
import math
from camera_transition import smoothstep


class Perspective:
    def __init__(self, *, distance=220., side=45., height=65., fov=90., duration=.45, third=False):
        limits = ((distance, 80, 400), (side, -120, 120), (height, 10, 160),
                  (fov, 65, 115), (duration, .15, 1.2))
        if any(isinstance(v, bool) or not math.isfinite(v) or not low <= v <= high
               for v, low, high in limits):
            raise ValueError('Invalid shoulder camera settings')
        self.distance, self.side, self.height, self.fov = distance, side, height, fov
        self.duration = duration
        self.amount = self.target = float(bool(third))
        self.started = None
        self.last_time = None
        self.current_side = side
        self.side_started = None

    def update(self, now):
        if not math.isfinite(now):
            raise ValueError('Invalid perspective clock')
        if self.last_time is not None and now < self.last_time:
            self.started = None  # Freeze at the current pose on clock rollback.
            self.side_started = None
        self.last_time = now
        if self.started is not None:
            t = (now-self.started)/self.travel_time
            self.amount = self.origin+(self.target-self.origin)*smoothstep(t)
            if t >= 1:
                self.amount, self.started = self.target, None
        if self.side_started is not None:
            t = (now-self.side_started)/self.side_travel_time
            self.current_side = self.side_origin+(self.side-self.side_origin)*smoothstep(t)
            if t >= 1:
                self.current_side, self.side_started = self.side, None
        return self.amount

    def select(self, third, now):
        self.update(now)
        target = float(bool(third))
        if target == self.target:
            return
        self.origin, self.target, self.started = self.amount, target, now
        self.travel_time = max(.001, self.duration*abs(target-self.amount))

    def swap_shoulder(self, now):
        """Reverse the chosen side, continuing smoothly from the current pose."""
        self.update(now)
        if self.side == 0:
            return False  # A centered camera has no shoulder displacement.
        self.side_origin = self.current_side
        self.side = -self.side
        self.side_started = now
        self.side_travel_time = max(.001, self.duration*abs(self.side-self.current_side)/(2*abs(self.side)))
        return True

    def offset(self, yaw, pitch, eye_height):
        if not all(math.isfinite(v) for v in (yaw, pitch, eye_height)) or abs(pitch) > 80:
            raise ValueError('Invalid shoulder camera orientation')
        y, p = math.radians(yaw), math.radians(pitch)
        back = -self.distance*math.cos(p)
        # Keep the camera above the character root when looking upward. This
        # is a ground-height bound, not a substitute for world collision tests.
        third = (back*math.cos(y)-self.current_side*math.sin(y),
                 back*math.sin(y)+self.current_side*math.cos(y),
                 max(10., self.height-self.distance*math.sin(p)))
        first = (0., 0., eye_height)
        return tuple(a+(b-a)*self.amount for a, b in zip(first, third))

    def field_of_view(self, first):
        return first+(self.fov-first)*self.amount

    @property
    def arms_only(self):
        # Reveal the full body once the camera has pulled away from the head;
        # hide it again before entering the head on the return journey.
        return self.amount*self.distance < 70.
