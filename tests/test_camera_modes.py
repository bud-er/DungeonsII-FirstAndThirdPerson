import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_modes import Perspective
from locomotion_facing import control_yaw


class Perspectives(unittest.TestCase):
    def test_first_pose_is_unchanged(self):
        p = Perspective()
        self.assertEqual(p.offset(127, 43, 55), (0, 0, 55))
        self.assertEqual(p.field_of_view(115), 115)
        self.assertTrue(p.arms_only)

    def test_shoulder_side_rotates_with_view_and_supports_left_shoulder(self):
        p = Perspective(third=True, side=-45)
        for actual, expected in zip(p.offset(90, 0, 55), (45, -220, 65)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(p.field_of_view(115), 90)
        self.assertFalse(p.arms_only)

    def test_transition_can_reverse_without_position_jump(self):
        p = Perspective(duration=.4)
        p.select(True, 0)
        self.assertAlmostEqual(p.update(.2), .5)
        before = p.offset(20, -10, 55)
        p.select(False, .2)
        self.assertEqual(p.offset(20, -10, 55), before)
        self.assertAlmostEqual(p.update(.3), .25)
        self.assertEqual(p.update(.41), 0)

    def test_second_selection_does_not_restart_transition(self):
        p = Perspective(duration=.4)
        p.select(True, 0); p.select(True, .2)
        self.assertEqual(p.update(.4), 1)

    def test_shoulder_swap_is_smooth_and_preserves_distance_height_and_fov(self):
        p = Perspective(third=True, side=75, distance=250, duration=.4)
        before = p.offset(0, 0, 55)
        self.assertTrue(p.swap_shoulder(0))
        self.assertEqual(p.offset(0, 0, 55), before)
        p.update(.2)
        self.assertEqual(p.offset(0, 0, 55), (-250, 0, 65))
        p.update(.4)
        self.assertEqual(p.offset(0, 0, 55), (-250, -75, 65))
        self.assertEqual(p.field_of_view(115), 90)
        self.assertEqual(p.target, 1)

    def test_repeated_swap_reverses_from_current_pose_without_a_jump(self):
        p = Perspective(third=True, side=-75, duration=.4)
        p.swap_shoulder(0); p.update(.1)
        before = p.offset(47, -20, 55)
        self.assertTrue(p.swap_shoulder(.1))
        self.assertEqual(p.offset(47, -20, 55), before)
        p.update(.5)
        self.assertEqual(p.current_side, -75)
        self.assertIsNone(p.side_started)

    def test_simultaneous_perspective_and_shoulder_changes_reverse_continuously(self):
        p = Perspective(side=75,duration=.4)
        p.select(True,0); p.swap_shoulder(0)
        self.assertEqual(p.offset(0,0,55),(0,0,55))
        p.update(.2)
        self.assertEqual(p.amount,.5)
        self.assertEqual(p.current_side,0)
        before = p.offset(47,-20,55)
        p.select(False,.2); p.swap_shoulder(.2)
        self.assertEqual(p.offset(47,-20,55),before)
        p.update(.41)
        self.assertEqual(p.offset(47,-20,55),(0,0,55))
        self.assertEqual(p.current_side,75)
        p.select(True,.41); p.update(.82)
        self.assertEqual(p.offset(0,0,55),(-220,75,65))

    def test_shoulder_choice_survives_first_person_and_normal_camera_recreation(self):
        p = Perspective(third=True, side=75, duration=.4)
        p.swap_shoulder(0); p.update(.4)
        p.select(False, .4); p.update(.8)
        self.assertEqual(p.offset(90, 0, 55), (0, 0, 55))
        p.select(True, .8); p.update(1.3)
        resumed = Perspective(third=True, side=p.side)
        self.assertEqual(resumed.offset(90, 0, 55), p.offset(90, 0, 55))

    def test_centered_camera_is_unchanged_and_clock_rollback_does_not_jump(self):
        p = Perspective(third=True, side=0)
        self.assertFalse(p.swap_shoulder(0))
        self.assertEqual(p.current_side, 0)
        p = Perspective(third=True, side=75)
        p.swap_shoulder(1); p.update(1.2)
        before = p.offset(15, -30, 55)
        p.update(1.1); p.update(3)
        self.assertEqual(p.offset(15, -30, 55), before)

    def test_looking_up_never_moves_camera_below_root_height_bound(self):
        p = Perspective(distance=400, height=10, third=True)
        self.assertGreaterEqual(p.offset(0, 80, 55)[2], 10)

    def test_invalid_configuration_and_pose_rejected(self):
        for key, value in (('distance', float('nan')), ('side', 121), ('height', -1),
                           ('duration', 0), ('fov', True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Perspective(**{key:value})
        with self.assertRaises(ValueError):
            Perspective().offset(0, math.inf, 55)

    def test_third_person_retains_idle_body_facing_but_attack_faces_camera(self):
        args = dict(eligible=True, movement_locked=False, roll_active=False, idle_follows_view=False)
        self.assertIsNone(control_yaw(30, (0,0), attack_active=False, **args))
        self.assertEqual(control_yaw(30, (0,0), attack_active=True, **args), 30)
        self.assertEqual(control_yaw(30, (0,-1), attack_active=False, **args), -150)


if __name__ == '__main__': unittest.main()
