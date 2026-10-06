from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from control_gestures import ClickGesture, DirectionDoubleTap, SprintIntent


class Gestures(unittest.TestCase):
    def clicks(self, count):
        g = ClickGesture()
        g.update(False, 0)
        results=[]
        for n in range(count):
            results += [g.update(True, .05+n*.13), g.update(False, .10+n*.13)]
        results.append(g.update(False, 1))
        return [r for r in results if r is not None]

    def test_single_double_triple_are_mutually_exclusive(self):
        for count in (1,2,3):
            with self.subTest(count=count): self.assertEqual(self.clicks(count), [count])

    def test_hold_does_not_trigger_single_double_or_repeat(self):
        g=ClickGesture(); g.update(False,0); g.update(True,.1)
        self.assertIsNone(g.update(True,.4))
        self.assertEqual(g.update(True,2.1),'hold')
        self.assertIsNone(g.update(True,5))
        self.assertIsNone(g.update(False,5.1))
        self.assertIsNone(g.update(False,6))

    def test_second_click_held_is_a_hold_not_a_quick_turn(self):
        g=ClickGesture(); g.update(False,0); g.update(True,.05); g.update(False,.1)
        self.assertIsNone(g.update(True,.15))
        self.assertEqual(g.update(True,2.16),'hold')
        self.assertIsNone(g.update(False,2.3))

    def test_startup_focus_loss_and_shift_click_cannot_create_taps(self):
        for reason in ('startup','focus','interaction'):
            g=ClickGesture()
            if reason!='startup':
                g.update(False,0); g.update(True,.1)
                g.update(True,.15,eligible=reason!='focus',consumed=reason=='interaction')
            self.assertIsNone(g.update(True,1))
            self.assertIsNone(g.update(False,1.1))
            self.assertIsNone(g.update(False,2))

    def test_keyboard_double_is_committed_on_release_without_triple_wait(self):
        g=ClickGesture(maximum=2); g.update(False,0)
        g.update(True,.05);g.update(False,.1);g.update(True,.15)
        self.assertEqual(g.update(False,.2),2)
        self.assertIsNone(g.update(False,1))

    def test_clock_rollback_cancels_pending_tap(self):
        g=ClickGesture();g.update(False,10);g.update(True,10.1);g.update(False,10.2)
        self.assertIsNone(g.update(False,1))
        self.assertIsNone(g.update(False,2))


class SprintInputs(unittest.TestCase):
    def test_matching_direction_requires_neutral_between_pushes(self):
        d=DirectionDoubleTap();d.update((0,0),0)
        self.assertFalse(d.update((0,1),.1))
        self.assertFalse(d.update((0,1),.15))
        d.update((0,0),.2)
        self.assertTrue(d.update((0,1),.3))

    def test_direction_change_and_analog_noise_do_not_sprint(self):
        d=DirectionDoubleTap();d.update((0,0),0);d.update((0,1),.1);d.update((0,0),.15)
        self.assertFalse(d.update((1,0),.2))
        d.update((0,0),.3)
        self.assertFalse(d.update((.4,0),.35))

    def test_backward_and_diagonal_double_flicks_work(self):
        for axes in ((0,-1),(-.70710678,.70710678)):
            d=DirectionDoubleTap();d.update((0,0),0);d.update(axes,.1);d.update((0,0),.15)
            self.assertTrue(d.update(axes,.2))

    def test_sprint_latches_only_until_stop_attack_or_focus_loss(self):
        for reason in ('stop','attack','focus'):
            s=SprintIntent()
            self.assertTrue(s.update((0,1),0,request=True))
            self.assertTrue(s.update((0,1),.5))
            self.assertFalse(s.update((0,0) if reason=='stop' else (0,1),1,
                                     blocked=reason=='attack',eligible=reason!='focus'))
            self.assertFalse(s.update((0,1),2))

    def test_stationary_request_expires_and_second_click_stops_sprint(self):
        s=SprintIntent();self.assertFalse(s.update((0,0),0,request=True))
        self.assertFalse(s.update((0,1),1))
        self.assertTrue(s.update((0,1),2,request=True))
        self.assertFalse(s.update((0,1),3,request=True))

    def test_disabled_direction_gesture_and_clock_rollback_cancel(self):
        s=SprintIntent();s.update((0,0),0,double_tap=False)
        s.update((0,1),.1,double_tap=False);s.update((0,0),.2,double_tap=False)
        self.assertFalse(s.update((0,1),.3,double_tap=False))
        self.assertTrue(s.update((0,1),10,request=True))
        self.assertFalse(s.update((0,1),1))


if __name__=='__main__':unittest.main()
