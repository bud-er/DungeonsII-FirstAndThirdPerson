"""Pure facing-policy checks; no process, memory, or input access."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from locomotion_facing import control_yaw


class LocomotionFacingTests(unittest.TestCase):
    def facing(self,yaw,axes,**options):
        flags=dict(eligible=True,movement_locked=False,attack_active=False,roll_active=False)
        flags.update(options)
        return control_yaw(yaw,axes,**flags)

    def test_travel_faces_camera_relative_cardinal_and_diagonal_directions(self):
        for yaw in (-179.,-90.,0.,38.,179.,398.):
            for axes,offset in (((0.,1.),0.),((0.,-1.),180.),((1.,0.),90.),
                                ((-1.,0.),-90.),((1.,1.),45.),((-1.,-1.),-135.)):
                with self.subTest(yaw=yaw,axes=axes):
                    self.assertAlmostEqual(self.facing(yaw,axes),(yaw+offset+180)%360-180)

    def test_analog_strength_does_not_change_bearing_above_neutral_threshold(self):
        self.assertEqual(self.facing(20.,(0.,-.11)),-160.)
        self.assertEqual(self.facing(20.,(0.,-1.)),-160.)
        self.assertAlmostEqual(self.facing(20.,(.3,.4)),56.86989764584405)
        self.assertAlmostEqual(self.facing(20.,(.6,.8)),56.86989764584405)

    def test_neutral_and_small_stick_noise_follow_view_without_remembering_previous_travel(self):
        self.assertEqual(self.facing(20.,(0.,-1.)),-160.)
        for axes in ((0.,0.),(-0.,0.),(0.,-.1),(.06,.08)):
            with self.subTest(axes=axes):
                self.assertEqual(self.facing(20.,axes),20.)
                self.assertEqual(self.facing(405.,axes),45.)

    def test_radial_threshold_does_not_treat_a_small_diagonal_as_neutral(self):
        self.assertEqual(self.facing(0.,(.08,.08)),45.)

    def test_held_or_active_attack_faces_view_while_backpedaling_or_strafing(self):
        for axes in ((0.,-1.),(-1.,0.),(1.,0.)):
            for locked in (False,True):
                with self.subTest(axes=axes,locked=locked):
                    self.assertEqual(self.facing(405.,axes,attack_active=True,movement_locked=locked),45.)

    def test_committed_roll_keeps_native_direction_even_if_attack_is_also_active(self):
        for axes in ((0.,0.),(0.,-1.),(1.,0.)):
            for attack in (False,True):
                with self.subTest(axes=axes,attack=attack):
                    self.assertIsNone(self.facing(75.,axes,roll_active=True,attack_active=attack))

    def test_focus_ui_or_other_ineligibility_preserves_native_control(self):
        for attack in (False,True):
            self.assertIsNone(self.facing(75.,(0.,-1.),eligible=False,attack_active=attack))

    def test_native_movement_lock_preserves_control_without_an_explicit_attack(self):
        for axes in ((0.,0.),(0.,-1.),(1.,0.)):
            self.assertIsNone(self.facing(75.,axes,movement_locked=True))

    def test_finishing_roll_returns_to_current_movement_or_view_without_timed_latch(self):
        self.assertIsNone(self.facing(10.,(0.,-1.),roll_active=True))
        self.assertEqual(self.facing(80.,(0.,-1.)),-100.)
        self.assertEqual(self.facing(90.,(0.,0.)),90.)

    def test_large_and_wraparound_view_angles_are_canonical(self):
        self.assertEqual(self.facing(180.,(0.,0.)),-180.)
        self.assertEqual(self.facing(-180.,(1.,0.)),-90.)
        self.assertEqual(self.facing(179.,(1.,0.)),-91.)
        self.assertEqual(self.facing(3600.,(0.,1.)),0.)

    def test_nonfinite_or_out_of_range_values_are_rejected_before_any_state_choice(self):
        cases=((math.nan,(0.,0.)),(math.inf,(0.,0.)),(0.,(math.nan,0.)),
               (0.,(0.,math.inf)),(0.,(1.01,0.)),(0.,(0.,-1.01)))
        for yaw,axes in cases:
            for flags in ({},{'eligible':False},{'roll_active':True},{'attack_active':True}):
                with self.subTest(yaw=yaw,axes=axes,flags=flags):
                    with self.assertRaisesRegex(RuntimeError,'Invalid locomotion facing input'):
                        self.facing(yaw,axes,**flags)


if __name__=='__main__':unittest.main()
