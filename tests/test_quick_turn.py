from pathlib import Path
import math
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from controller_hold import CameraHold, LEFT_THUMB
from quick_turn import QuickTurn


class QuickTurnTests(unittest.TestCase):
    def turn(self):
        turn=QuickTurn()
        turn.update(0,0.)
        return turn

    def start(self,turn):
        turn.update(LEFT_THUMB,1.)
        turn.update(0,1.06)
        self.assertEqual(turn.update(LEFT_THUMB,1.2),0.)
        self.assertTrue(turn.triggered)

    def test_second_click_starts_one_turn_and_held_button_never_repeats(self):
        turn=self.turn();self.start(turn)
        self.assertEqual(turn.update(LEFT_THUMB,1.48),180.)
        for time in (2.,3.,8.):
            self.assertEqual(turn.update(LEFT_THUMB,time),0.)
            self.assertFalse(turn.triggered)

    def test_animation_is_smooth_monotonic_and_exact_at_different_frame_rates(self):
        for fps in (30,60,120,144):
            turn=self.turn();self.start(turn)
            samples=[turn.update(0,1.2+i/fps) for i in range(1,math.ceil(turn.duration*fps)+1)]
            self.assertAlmostEqual(sum(samples),180.)
            self.assertTrue(all(v>=0 for v in samples))
            self.assertLess(samples[0],samples[len(samples)//2])
            self.assertFalse(turn.active)
            self.assertEqual(turn.update(0,2.),0.)

    def test_one_click_and_slow_double_click_do_not_turn(self):
        turn=self.turn();turn.update(LEFT_THUMB,1.);turn.update(0,1.05)
        self.assertEqual(turn.update(0,1.5),0.)
        self.assertEqual(turn.update(LEFT_THUMB,1.6),0.)
        self.assertFalse(turn.triggered)
        self.assertFalse(turn.active)

    def test_long_first_press_is_not_a_double_click(self):
        turn=self.turn();turn.update(LEFT_THUMB,1.);turn.update(0,1.3)
        turn.update(LEFT_THUMB,1.31)
        self.assertFalse(turn.triggered)
        self.assertFalse(turn.active)

    def test_two_second_hold_still_toggles_without_a_turn(self):
        turn=self.turn();hold=CameraHold();hold.update(0,0.)
        for time in (1.,2.,2.99):
            self.assertFalse(hold.update(LEFT_THUMB,time))
            self.assertEqual(turn.update(LEFT_THUMB,time),0.)
        self.assertTrue(hold.update(LEFT_THUMB,3.))
        self.assertEqual(turn.update(LEFT_THUMB,3.),0.)

    def test_accepted_double_click_cancels_hold_until_button_released(self):
        turn=self.turn();hold=CameraHold();hold.update(0,0.)
        for button,time in ((LEFT_THUMB,1.),(0,1.06),(LEFT_THUMB,1.2),(LEFT_THUMB,1.3),
                            (LEFT_THUMB,1.5),(LEFT_THUMB,4.)):
            self.assertFalse(hold.update(button,time))
            turn.update(button,time)
            if turn.triggered or turn.active:hold.cancel()
        turn.update(0,4.1);hold.update(0,4.1)
        hold.update(LEFT_THUMB,4.2)
        self.assertTrue(hold.update(LEFT_THUMB,6.2))

    def test_startup_held_or_controller_change_requires_release(self):
        turn=QuickTurn()
        turn.update(LEFT_THUMB,1.);turn.update(LEFT_THUMB,1.1)
        self.assertFalse(turn.active)
        turn.update(0,1.2);turn.update(LEFT_THUMB,1.3);turn.update(0,1.35)
        turn.cancel();turn.update(LEFT_THUMB,1.4)
        self.assertFalse(turn.triggered)

    def test_first_tap_in_third_person_or_menu_cannot_trigger_after_return(self):
        for state in ('third person','menu','focus loss'):
            with self.subTest(state=state):
                turn=self.turn();turn.update(LEFT_THUMB,1.,eligible=False)
                turn.update(0,1.05,eligible=False)
                turn.update(LEFT_THUMB,1.15,eligible=True)
                self.assertFalse(turn.triggered)
                self.assertFalse(turn.active)

    def test_losing_eligibility_stops_animation_at_current_angle_without_delayed_jump(self):
        turn=self.turn();self.start(turn)
        partial=turn.update(0,1.2+turn.duration/2.)
        self.assertAlmostEqual(partial,90.)
        self.assertEqual(turn.update(0,1.35,eligible=False),0.)
        self.assertFalse(turn.active)
        self.assertEqual(turn.update(0,2.),0.)

    def test_clicks_during_sweep_are_not_queued(self):
        turn=self.turn();self.start(turn)
        for button,time in ((0,1.23),(LEFT_THUMB,1.27),(0,1.3),(LEFT_THUMB,1.32),(0,1.5)):
            turn.update(button,time)
            self.assertFalse(turn.triggered)
        self.assertEqual(turn.update(LEFT_THUMB,1.55),0.)
        self.assertFalse(turn.triggered)

    def test_release_and_second_pair_can_trigger_another_turn(self):
        turn=self.turn();self.start(turn)
        turn.update(0,1.5);turn.update(LEFT_THUMB,2.);turn.update(0,2.06)
        turn.update(LEFT_THUMB,2.2)
        self.assertTrue(turn.triggered)
        self.assertEqual(turn.update(0,2.5),180.)

    def test_yaw_wrap_and_concurrent_look_preserve_180_degree_offset(self):
        turn=self.turn();self.start(turn);yaw=170.;pitch=-35.
        for time,look in ((1.25,2.),(1.34,3.),(1.4,-1.),(1.5,0.)):
            yaw=(yaw+turn.update(0,time)+look+180.)%360.-180.
        self.assertAlmostEqual(yaw,-6.)
        self.assertEqual(pitch,-35.)

    def test_other_buttons_cannot_turn(self):
        turn=self.turn()
        for button,time in ((0x80,1.),(0,1.05),(0x80,1.15),(0,1.4)):
            self.assertEqual(turn.update(button,time),0.)
            self.assertFalse(turn.triggered)

    def test_clock_rollback_cancels_instead_of_reversing_camera(self):
        turn=self.turn();self.start(turn);turn.update(0,1.34)
        self.assertEqual(turn.update(0,1.3),0.)
        self.assertFalse(turn.active)
        self.assertEqual(turn.update(0,2.),0.)

    def test_nonfinite_clock_and_invalid_timing_rejected(self):
        with self.assertRaises(ValueError):self.turn().update(0,float('nan'))
        for options in (dict(duration=0),dict(click_window=-1),dict(max_tap=float('inf')),
                        dict(click_window=.1,max_tap=.2)):
            with self.assertRaises(ValueError):QuickTurn(**options)

    def test_requested_speed_is_fifteen_percent_faster(self):
        self.assertAlmostEqual(.28/self.turn().duration,1.15)


if __name__=='__main__':unittest.main()
