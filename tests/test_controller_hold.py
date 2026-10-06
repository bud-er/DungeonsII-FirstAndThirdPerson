from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from controller_hold import CameraHold, LEFT_THUMB


class ControllerHoldTests(unittest.TestCase):
    def make_hold(self):
        hold = CameraHold()
        hold.update(0, 0)
        return hold

    def test_two_seconds_and_one_toggle_until_release(self):
        hold = self.make_hold()
        self.assertFalse(hold.update(LEFT_THUMB, 10))
        self.assertFalse(hold.update(LEFT_THUMB, 11.999))
        self.assertTrue(hold.update(LEFT_THUMB, 12))
        self.assertFalse(hold.update(LEFT_THUMB, 20))
        hold.update(0, 21)
        hold.update(LEFT_THUMB, 22)
        self.assertTrue(hold.update(LEFT_THUMB, 24))

    def test_short_presses_do_not_accumulate(self):
        hold = self.make_hold()
        for t in (0, 2, 4):
            self.assertFalse(hold.update(LEFT_THUMB, t))
            self.assertFalse(hold.update(LEFT_THUMB, t+1.9))
            hold.update(0, t+1.95)

    def test_focus_loss_requires_release_before_another_hold(self):
        hold = self.make_hold()
        hold.update(LEFT_THUMB, 1)
        hold.update(LEFT_THUMB, 2, eligible=False)
        self.assertFalse(hold.update(LEFT_THUMB, 10))
        hold.update(0, 11)
        hold.update(LEFT_THUMB, 12)
        self.assertTrue(hold.update(LEFT_THUMB, 14))

    def test_startup_held_button_is_ignored(self):
        hold = CameraHold()
        self.assertFalse(hold.update(LEFT_THUMB, 0))
        self.assertFalse(hold.update(LEFT_THUMB, 10))

    def test_reset_or_controller_change_cancels_pending_toggle(self):
        hold = self.make_hold()
        hold.update(LEFT_THUMB, 1)
        hold.cancel()
        self.assertFalse(hold.update(LEFT_THUMB, 5))

    def test_other_buttons_cannot_toggle(self):
        hold = self.make_hold()
        self.assertFalse(hold.update(0x0080, 1))
        self.assertFalse(hold.update(0x0080, 4))

    def test_clock_rollback_cancels_hold(self):
        hold = self.make_hold()
        hold.update(LEFT_THUMB, 10)
        hold.update(LEFT_THUMB, 9)
        self.assertFalse(hold.update(LEFT_THUMB, 20))


if __name__ == '__main__':
    unittest.main()
