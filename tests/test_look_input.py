from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from look_input import LookClock
from external_camera import CameraSession
import external_camera as camera


class LookSampling(unittest.TestCase):
    def test_launcher_interactive_path_enables_the_confirmed_look_fix(self):
        with patch.object(camera,'SingleInstance'),patch.object(camera,'run_live') as run:
            camera.main(['--interactive'])
        args=run.call_args.args[0]
        self.assertTrue(args.sync_first_person_view)
        self.assertTrue(args.interleave_first_person_look)

    def test_controller_rotation_uses_elapsed_time_once_across_subsamples(self):
        look=LookClock();look.sample(10.,(0,0),(1,0),.12)
        values=[look.sample(t,(0,0),(1,0),.12)[0] for t in (10.004,10.01,10.017,10.02)]
        self.assertAlmostEqual(sum(values),3.)

    def test_mouse_motion_is_not_scaled_by_subsample_time(self):
        look=LookClock()
        values=[look.sample(t,xy,(0,0),.12) for t,xy in ((1.,(2,3)),(1.001,(-1,2)),(1.006,(4,-1)))]
        self.assertAlmostEqual(sum(x[0] for x in values),.6)
        self.assertAlmostEqual(sum(x[1] for x in values),-.48)

    def test_focus_loss_discards_controller_time_and_cursor_jump(self):
        look=LookClock();look.sample(1.,(0,0),(1,0),.12);look.reset()
        self.assertEqual(look.sample(30.,(500,500),(1,1),.12,discard=True),(0.,0.))
        self.assertAlmostEqual(look.sample(30.01,(0,0),(1,0),.12)[0],1.5)

    def test_long_stall_is_bounded(self):
        look=LookClock();look.sample(1.,(0,0),(1,0),.12)
        self.assertEqual(look.sample(4.,(0,0),(1,0),.12),(7.5,0.))

    def session(self):
        s=CameraSession.__new__(CameraSession)
        s.avatar_profile=Mock(applied=True,follow_view=True)
        s.pose=Mock();s.look_refresh=Mock(return_value=True)
        s.blend=s.blend_target=1.;s.input_paused=s.dialogue_active=False
        s.perspective=SimpleNamespace(amount=0.,target=0.)
        s.local_scope=Mock();s.pawn=100;s.camera=200;s.pointer=Mock(return_value=200)
        s.view_target_profile=Mock();s.sync_first_person_view=True
        return s

    def test_refresh_updates_camera_and_heading_without_rescanning_equipment(self):
        s=self.session();s.refresh_first_person_look()
        s.pose.update.assert_called_once_with(1.)
        s.avatar_profile.update_heading.assert_called_once_with()
        s.avatar_profile.update.assert_not_called()
        s.view_target_profile.sync_heading.assert_called_once_with(True)

    def test_no_extra_look_in_menus_shoulder_transitions_or_native_facing(self):
        for mode in ('menu','dialogue','shoulder','fade','native'):
            with self.subTest(mode=mode):
                s=self.session()
                if mode=='menu':s.input_paused=True
                if mode=='dialogue':s.dialogue_active=True
                if mode=='shoulder':s.perspective.target=1.
                if mode=='fade':s.blend=.7
                if mode=='native':s.avatar_profile.follow_view=False
                s.refresh_first_person_look()
                s.look_refresh.assert_not_called();s.pose.update.assert_not_called()

    def test_changed_camera_prevents_look_sampling_and_all_pose_writes(self):
        s=self.session();s.pointer.return_value=999
        with self.assertRaisesRegex(RuntimeError,'Camera changed'):s.refresh_first_person_look()
        s.look_refresh.assert_not_called();s.pose.update.assert_not_called()

    def test_focus_guard_rejection_does_not_write_pose(self):
        s=self.session();s.look_refresh.return_value=False;s.refresh_first_person_look()
        s.pose.update.assert_not_called();s.avatar_profile.update_heading.assert_not_called()


if __name__=='__main__':unittest.main()
