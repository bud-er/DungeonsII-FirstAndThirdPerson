import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from camera_transition import smoothstep, slerp, unreal_rotator
from avatar_profile import yaw_quaternion


class CameraTransition(unittest.TestCase):
    def test_easing_is_bounded_and_monotonic(self):
        values = [smoothstep(i/100) for i in range(-10, 111)]
        self.assertEqual(values, sorted(values))
        self.assertEqual((values[0], values[-1]), (0, 1))
        self.assertAlmostEqual(smoothstep(.5), .5)
        self.assertLess(smoothstep(.01), .001)
        self.assertGreater(smoothstep(.99), .999)

    def test_rotation_crosses_180_by_the_short_path(self):
        midpoint = slerp(yaw_quaternion(170), yaw_quaternion(-170), .5)
        self.assertAlmostEqual(abs(unreal_rotator(midpoint)[1]), 180)
        self.assertAlmostEqual(sum(v*v for v in midpoint), 1)

    def test_antipodal_quaternions_do_not_spin(self):
        a = yaw_quaternion(50)
        actual = slerp(a, tuple(-v for v in a), .5)
        self.assertAlmostEqual(unreal_rotator(actual)[1], 50)

    def test_unreal_pitch_convention_matches_camera(self):
        pitch, yaw = math.radians(-60)/2, math.radians(45)/2
        q = (math.sin(pitch)*math.sin(yaw), -math.sin(pitch)*math.cos(yaw),
             math.cos(pitch)*math.sin(yaw), math.cos(pitch)*math.cos(yaw))
        for actual, expected in zip(unreal_rotator(q), (-60, 45, 0)):
            self.assertAlmostEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
