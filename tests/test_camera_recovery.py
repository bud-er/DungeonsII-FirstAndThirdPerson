import contextlib
import io
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import external_camera as camera
from camera_recovery import CameraRecovery, failure_path
from test_external_camera import ImmediateDiscovery


class RecoveryTests(unittest.TestCase):
    def test_failure_path_identifies_origin_without_full_install_path(self):
        def unreadable_record():
            raise OSError('memory unavailable')
        try:
            unreadable_record()
        except OSError as error:
            location=failure_path(error)
        self.assertIn('test_camera_recovery.py:',location)
        self.assertIn('unreadable_record',location)
        self.assertNotIn(str(Path(__file__).parent),location)
        self.assertEqual(failure_path(RuntimeError('no traceback')),'')

    def test_repeated_failures_back_off_and_stop_without_losing_manual_retry(self):
        recovery=CameraRecovery()
        for now,delay in ((1.,.5),(2.,1.),(4.,2.)):
            self.assertTrue(recovery.failed(now))
            self.assertEqual(recovery.retry_at,now+delay)
        self.assertFalse(recovery.failed(7.))
        recovery.reset()
        self.assertTrue(recovery.failed(8.))

    def test_one_good_frame_cannot_rearm_an_endless_failure_loop(self):
        recovery=CameraRecovery();recovery.failed(1.)
        recovery.healthy(2.);recovery.healthy(3.)
        self.assertEqual(recovery.attempts,1)
        recovery.failed(4.);recovery.healthy(5.);recovery.healthy(15.)
        self.assertEqual(recovery.attempts,0)

    def flow(self, *, persistent_error=False, restore_blocked=False, manual_retry=False):
        frame=[0];created=[];active=[];updates=[]
        controls,game=Mock(),Mock()
        controls.foreground.return_value=123
        controls.gamepad.return_value=(0.,0.,0)
        controls.mouse.return_value=(0.,0.)
        controls.pump.side_effect=lambda:frame.__setitem__(0,frame[0]+1)
        controls.pressed.side_effect=lambda key:(key==0x79 and frame[0]==75) or (manual_retry and key==0x75 and frame[0]==55)
        def alive(handle,out):out._obj.value=259;return True
        game.kernel.GetExitCodeProcess.side_effect=alive
        def factory(*args,**kwargs):
            view=Mock(pc=100,pawn=200,yaw=80.,pitch=-20.,blend=1.,blend_target=1.,restore_failures=set(),damage_feedback=None)
            view.enable_controller_profile.side_effect=lambda:active.append(view)
            def update():
                updates.append(view)
                if persistent_error or (view is active[0] and len(updates)==2):
                    raise RuntimeError('cursor changed')
            view.update.side_effect=update
            def restore():
                if restore_blocked and view is active[0] and not (manual_retry and frame[0]>=55):
                    view.restore_failures={'native cursor'}
                else:view.restore_failures=set()
            view.restore.side_effect=restore
            created.append(view)
            return view
        args=SimpleNamespace(seconds=0,start_enabled=True,height=55,fov=115,sensitivity=.12,
                             no_avatar=True,no_damage_flash=True,keep_mob_silhouettes=True)
        with patch.object(camera,'NativeInput',return_value=controls), \
             patch.object(camera,'CameraDiscovery',ImmediateDiscovery), \
             patch.object(camera,'CameraSession',side_effect=factory) as sessions, \
             patch.object(camera,'MenuState') as menus, \
             patch.object(camera.time,'monotonic',side_effect=lambda:frame[0]*.1), \
             patch.object(camera.time,'sleep'),contextlib.redirect_stdout(io.StringIO()) as output:
            menus.side_effect=lambda seed:Mock(session=seed,reason=Mock(return_value=None),input_captured=False)
            camera.interactive(game,args)
        return active,created,sessions,output.getvalue()

    def test_transient_failure_restores_then_discovers_fresh_and_resumes_without_f6(self):
        active,created,factory,output=self.flow()
        self.assertEqual(len(active),2)
        active[0].restore.assert_called_once()
        self.assertIn('Camera recovery 1/3',output)
        self.assertIn('Failure path:',output)
        # Recovery's read-only seed discards the failed session's object cache.
        self.assertIsNone(factory.call_args_list[2].kwargs['previous'])
        self.assertIsNot(created[0],created[2])
        self.assertGreater(active[1].update.call_count,1)

    def test_persistent_failure_stops_after_three_automatic_reactivations(self):
        active,_,_,output=self.flow(persistent_error=True)
        self.assertEqual(len(active),4)
        self.assertEqual(sum(view.update.call_count for view in active),4)
        self.assertIn('paused after repeated failures',output)

    def test_failed_restore_never_allows_another_activation(self):
        active,_,_,output=self.flow(restore_blocked=True)
        self.assertEqual(len(active),1)
        self.assertIn('no new camera writes allowed',output)
        self.assertIn('paused after repeated failures',output)

    def test_f6_retries_an_existing_restore_and_resumes_when_it_becomes_clean(self):
        active,_,_,_=self.flow(restore_blocked=True,manual_retry=True)
        self.assertEqual(len(active),2)
        self.assertGreaterEqual(active[0].restore.call_count,5)

    def test_retirement_uses_saved_serial_slot_without_dereferencing_freed_object(self):
        session=camera.CameraSession.__new__(camera.CameraSession)
        game=session.game=Mock(base=0x100000, build_profile='steam-25647713')
        expected=(struct.pack('<I',9)+bytes(16),struct.pack('<I',27))
        descriptor=bytearray(0x30)
        struct.pack_into('<Q',descriptor,0x10,0x200000)
        struct.pack_into('<IIII',descriptor,0x20,200,100,2,1)
        game.u64.return_value=0x300000
        item=bytearray(24);struct.pack_into('<Q',item,0,0x999);item[0x10:0x14]=expected[1]
        game.read.side_effect=lambda address,size:bytes(descriptor) if size==0x30 else bytes(item)
        self.assertTrue(session.object_retired(0x555,expected))
        self.assertNotIn(0x555,[call.args[0] for call in game.read.call_args_list])
        game.read.side_effect=OSError('slot unreadable')
        with self.assertRaises(OSError):session.object_retired(0x555,expected)


if __name__=='__main__':unittest.main()
