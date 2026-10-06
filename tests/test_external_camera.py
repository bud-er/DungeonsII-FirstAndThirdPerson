import contextlib
import ctypes as C
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import external_camera as camera


class ImmediateDiscovery:
    def __init__(self):self.result=None
    def start(self,factory):self.result=factory()
    def poll(self):
        result,self.result=self.result,None
        return result,None
    def close(self):pass


class ExternalCameraRestoration(unittest.TestCase):
    def test_quarantined_native_cli_is_rejected_before_opening_the_game(self):
        with patch.object(camera, 'CameraMemory') as memory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as rejected:
                camera.main(['--interactive', '--native-roll-hook'])
        self.assertEqual(rejected.exception.code, 2)
        memory.assert_not_called()

    def test_failed_target_data_cli_is_rejected_before_opening_the_game(self):
        with patch.object(camera,'CameraMemory') as memory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as rejected:
                camera.main(['--interactive','--roll-target-data'])
        self.assertEqual(rejected.exception.code,2)
        memory.assert_not_called()

    def make_session(self):
        session = camera.CameraSession.__new__(camera.CameraSession)
        session.game = Mock()
        session.identities = {100: ('same', 1)}
        session.identity = Mock(return_value=('same', 1))
        session.originals = {120: [100, b'\x01', 1]}
        return session

    def test_restore_preserves_bits_changed_by_game(self):
        session = self.make_session()
        session.game.read.return_value = b'\x82'
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        session.game.write.assert_called_once_with(120, b'\x83')

    def ui_session(self):
        session = self.make_session()
        storage = {120:b'\x82', 130:b'\0', 200:b'camera'}
        session.game.read.side_effect = lambda address, size: storage[address]
        session.game.write.side_effect = lambda address, data: storage.__setitem__(address, data)
        session.originals[130] = [100, b'\x01', None]
        session.cursor_fields = {120, 130}
        session.plan = [(120, 0, 1), (130, b'\0', None), (200, b'camera', None)]
        session.input_paused = False
        session.controller_profile = Mock(restore_failures=0)
        for name in ('keyboard_movement', 'mouse_bindings', 'combat_profile', 'cursor_profile'):
            setattr(session, name, Mock(restore_failures=set()))
        session.native_roll_profile = None
        return session, storage

    def test_small_ui_handoff_restores_input_once_and_preserves_other_cursor_bits(self):
        session, storage = self.ui_session()
        with contextlib.redirect_stdout(io.StringIO()):
            session.set_ui_input(True)
            session.set_ui_input(True)
        self.assertTrue(session.input_paused)
        self.assertEqual(storage[120], b'\x83')
        self.assertEqual(storage[130], b'\x01')
        session.controller_profile.set_ui_input.assert_called_once_with(True)
        session.controller_profile.restore.assert_not_called()
        session.keyboard_movement.restore.assert_called_once()
        session.cursor_profile.restore.assert_called_once()
        with contextlib.redirect_stdout(io.StringIO()):
            session.set_ui_input(False)
        self.assertFalse(session.input_paused)
        session.controller_profile.set_ui_input.assert_called_with(False)
        self.assertEqual(session.mouse_bindings.next_collect, 0.)

    def test_small_ui_disables_roll_target_data_before_native_menu_input(self):
        session,_=self.ui_session()
        session.roll_target_profile=Mock(restore_failures=set())
        with contextlib.redirect_stdout(io.StringIO()):session.set_ui_input(True)
        session.roll_target_profile.restore.assert_called_once()

    def test_ui_handoff_preserves_native_menu_cursor_changes(self):
        session, storage = self.ui_session()
        # The baseline came from controller play, where the cursor was hidden.
        # The new menu has already requested a visible hand cursor.
        session.originals[120] = [100, b'\x00', 1]
        storage[120], storage[130] = b'\x83', b'\x09'
        with contextlib.redirect_stdout(io.StringIO()):
            session.set_ui_input(True)
        self.assertEqual(storage[120], b'\x83')
        self.assertEqual(storage[130], b'\x09')
        session.game.write.assert_not_called()

    def test_camera_rotation_is_applied_before_binding_and_scene_work(self):
        session,_=self.ui_session()
        session.pc,session.pawn,session.camera=100,101,102
        session.pointer=lambda obj,cls,name:101 if name=='Pawn' else 102
        session.blend,session.blend_target,session.blend_started=1.,1.,None
        session.pose,session.avatar_profile,session.silhouette_profile=Mock(),None,Mock()
        order=[]
        session.pose.update.side_effect=lambda _:order.append('camera')
        session.controller_profile.update.side_effect=lambda:order.append('bindings')
        session.silhouette_profile.update.side_effect=lambda:order.append('scene')
        session.update()
        self.assertEqual(order,['camera','bindings','scene'])

    def test_failed_ui_handoff_remains_a_reported_restoration_error(self):
        session, storage = self.ui_session()
        session.keyboard_movement.restore_failures = {'failed'}
        with self.assertRaisesRegex(RuntimeError, 'restoration failed'):
            session.set_ui_input(True)
        self.assertFalse(session.input_paused)
        session.game.write.assert_not_called()

    def test_first_person_arms_use_current_camera_sample_before_slow_input_work(self):
        session,_=self.ui_session()
        session.pc,session.pawn,session.camera=100,101,102
        session.pointer=lambda obj,cls,name:101 if name=='Pawn' else 102
        session.blend,session.blend_target,session.blend_started=1.,1.,None
        session.pose,session.avatar_profile=Mock(),Mock(applied=True)
        visible={'camera':0.,'arms':0.}
        session.pose.update.side_effect=lambda _:visible.update(camera=session.yaw)
        session.avatar_profile.update.side_effect=lambda:visible.update(arms=session.yaw)
        # A native frame can run while expensive binding scans are in progress.
        def observe_frame():
            self.assertEqual(visible['camera'],session.yaw)
            self.assertEqual(visible['arms'],session.yaw)
        session.controller_profile.update.side_effect=observe_frame
        for session.yaw in (45.,-90.,179.,-179.):
            session.update()

    def test_shoulder_transition_restores_avatar_before_input_work(self):
        session,_=self.ui_session()
        session.pc,session.pawn,session.camera=100,101,102
        session.pointer=lambda obj,cls,name:101 if name=='Pawn' else 102
        session.blend,session.blend_target,session.blend_started=1.,1.,None
        session.perspective=Mock(arms_only=False)
        session.pose,session.avatar_profile=Mock(),Mock(applied=True,restore_failures=set())
        session.controller_profile.update.side_effect=lambda:session.avatar_profile.restore.assert_called_once()
        session.update()
        session.avatar_profile.update.assert_not_called()

    def test_small_ui_keeps_camera_pose_active_without_input_or_cursor_writes(self):
        session, storage = self.ui_session()
        with contextlib.redirect_stdout(io.StringIO()):
            session.set_ui_input(True)
        session.game.write.reset_mock()
        session.pc, session.pawn, session.camera = 100, 101, 102
        session.pointer = lambda obj, cls, name: 101 if name == 'Pawn' else 102
        session.blend, session.blend_target, session.blend_started = 1., 1., None
        session.pose, session.avatar_profile = Mock(), Mock()
        session.update()
        session.game.write.assert_called_once_with(200, b'camera')
        session.pose.update.assert_called_once_with(1.)
        session.avatar_profile.update.assert_called_once()
        session.controller_profile.update.assert_called_once()
        for name in ('keyboard_movement', 'mouse_bindings', 'combat_profile', 'cursor_profile'):
            getattr(session, name).update.assert_not_called()

    def test_recycled_object_is_never_written(self):
        session = self.make_session()
        session.identity.return_value = ('same', 2)
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        session.game.write.assert_not_called()

    def test_process_exit_during_restoration_is_handled(self):
        session = self.make_session()
        session.identity.side_effect = OSError('process exited')
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        session.game.write.assert_not_called()

    def test_update_failure_still_restores(self):
        fake = Mock()
        fake.update.side_effect = RuntimeError('simulated native read failure')
        with patch.object(camera, 'CameraMemory') as memory, patch.object(camera, 'CameraSession', return_value=fake), \
             patch.object(camera, 'SingleInstance'), \
             patch.object(sys, 'argv', ['external_camera.py', '--seconds', '1']), contextlib.redirect_stdout(io.StringIO()):
            memory.return_value.__enter__.return_value.pid = 1
            with self.assertRaises(RuntimeError):
                camera.main()
        fake.restore.assert_called_once()

    def run_interactive(self, *, lose_focus=False, update_error=False, launcher_stop=False):
        fake = Mock(yaw=0, pitch=0, blend=1., blend_target=1., restore_failures=set())
        if update_error:
            fake.update.side_effect = RuntimeError('object changed')
        inputs = Mock()
        frame = [0]
        inputs.pump.side_effect = lambda: frame.__setitem__(0, frame[0] + 1)
        inputs.pressed.side_effect = lambda key: key == 0x79 and frame[0] == 3
        inputs.foreground.side_effect = [123, None if lose_focus else 123, 123]
        inputs.gamepad.return_value = (0, 0, 0)
        inputs.mouse.return_value = (0, 0)
        game = Mock()

        def alive(handle, code):
            code._obj.value = 259
            return True

        game.kernel.GetExitCodeProcess.side_effect = alive
        args = SimpleNamespace(seconds=0, start_enabled=True, height=55, fov=95, sensitivity=.12)
        with patch.object(camera, 'NativeInput', return_value=inputs), \
             patch.object(camera, 'CameraDiscovery', ImmediateDiscovery), \
             patch.object(camera, 'CameraSession', return_value=fake), \
             patch.object(camera, 'MenuState') as menus, \
             patch.object(camera.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            menus.return_value.reason.return_value = None
            stop = Mock() if launcher_stop else None
            if stop is not None:
                stop.is_set.side_effect = lambda: frame[0]>=2
            camera.interactive(game, args, stop_event=stop)
        inputs.close.assert_called_once()
        fake.restore.assert_called_once()
        if lose_focus:
            self.assertEqual(inputs.mouse.call_count, 2)  # Initial discard + one focused look tick.
        return fake

    def test_interactive_quit_restores(self):
        self.assertEqual(self.run_interactive().update.call_count, 2)

    def test_launcher_stop_restores_active_camera(self):
        self.assertEqual(self.run_interactive(launcher_stop=True).update.call_count, 2)

    def test_launcher_stop_while_waiting_never_opens_game(self):
        stop = Mock()
        stop.is_set.return_value = True
        with patch.object(camera, 'NativeInput'), patch.object(camera, 'CameraMemory') as memory, \
             contextlib.redirect_stdout(io.StringIO()):
            camera.run_live(SimpleNamespace(start_enabled=False), stop_event=stop)
        memory.assert_not_called()

    def test_quit_hotkey_is_serviced_during_pending_read_only_discovery(self):
        inputs,game,discovery=Mock(),Mock(),Mock()
        frame=[0]
        inputs.foreground.return_value=123
        inputs.gamepad.return_value=(0,0,0)
        inputs.pump.side_effect=lambda:frame.__setitem__(0,frame[0]+1)
        inputs.pressed.side_effect=lambda key:key==0x79 and frame[0]==3
        discovery.poll.return_value=(None,None)
        def alive(handle,code):
            code._obj.value=259;return True
        game.kernel.GetExitCodeProcess.side_effect=alive
        args=SimpleNamespace(seconds=0,start_enabled=False,height=55,fov=95,sensitivity=.12)
        with patch.object(camera,'NativeInput',return_value=inputs), \
             patch.object(camera,'CameraDiscovery',return_value=discovery), \
             patch.object(camera,'CameraSession') as session, \
             patch.object(camera.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            camera.interactive(game,args)
        session.assert_not_called()
        discovery.close.assert_called_once();inputs.close.assert_called_once()

    def test_brief_focus_loss_stops_look_without_flashing_the_normal_camera(self):
        self.assertEqual(self.run_interactive(lose_focus=True).update.call_count, 2)

    def test_interactive_update_failure_suspends_and_restores(self):
        self.assertEqual(self.run_interactive(update_error=True).update.call_count, 1)

    def test_l3_hold_toggles_on_and_off_through_the_normal_session_lifecycle(self):
        fake = Mock(yaw=0, pitch=0, blend=1., blend_target=1., restore_failures=set())
        inputs, game = Mock(), Mock()
        frame, clock = [0], [0.]
        inputs.gamepad_index = 0
        inputs.pump.side_effect = lambda: frame.__setitem__(0, frame[0]+1)
        inputs.pressed.side_effect = lambda key: key == 0x79 and frame[0] == 13
        inputs.foreground.return_value = 123
        inputs.mouse.return_value = (0,0)
        # Release at startup; hold 2 s, release, then hold another 2 s.
        inputs.gamepad.side_effect = lambda: (0,0,0 if frame[0] in (1,7) else 0x40)
        def alive(handle, code):
            code._obj.value=259
            return True
        game.kernel.GetExitCodeProcess.side_effect=alive
        args=SimpleNamespace(seconds=0,start_enabled=False,height=55,fov=95,sensitivity=.12)
        with patch.object(camera,'NativeInput',return_value=inputs), \
             patch.object(camera,'CameraDiscovery',ImmediateDiscovery), \
             patch.object(camera,'CameraSession',return_value=fake), \
             patch.object(camera,'MenuState') as menus, \
             patch.object(camera.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(camera.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+.5)), \
             contextlib.redirect_stdout(io.StringIO()):
            menus.return_value.reason.return_value=None
            camera.interactive(game,args)
        fake.enable_combat_profile.assert_called_once_with(steer_roll=True, filter_ranged=False)
        fake.animate_out.assert_called_once()
        fake.restore.assert_called_once()
        self.assertEqual(fake.update.call_count,6)

    def test_l3_double_click_turns_view_without_toggling_camera_and_preserves_pitch(self):
        fake=Mock(yaw=170.,pitch=-35.,blend=1.,blend_target=1.,restore_failures=set(),damage_feedback=None)
        inputs,game=Mock(),Mock()
        frame,views=[0],[]
        inputs.gamepad_index=0
        inputs.pump.side_effect=lambda:frame.__setitem__(0,frame[0]+1)
        # The second click waits out the optional third-click camera gesture.
        inputs.pressed.side_effect=lambda key:key==0x79 and frame[0]==20
        inputs.foreground.return_value=123
        inputs.mouse.return_value=(0,0)
        inputs.gamepad.side_effect=lambda:(0,0,0x40 if frame[0] in (2,4) else 0)
        fake.update.side_effect=lambda:views.append((fake.yaw,fake.pitch))
        def alive(handle,code):
            code._obj.value=259;return True
        game.kernel.GetExitCodeProcess.side_effect=alive
        args=SimpleNamespace(seconds=0,start_enabled=True,height=55,fov=115,sensitivity=.12,
                             no_avatar=True,no_damage_flash=True,keep_mob_silhouettes=True)
        with patch.object(camera,'NativeInput',return_value=inputs), \
             patch.object(camera,'CameraDiscovery',ImmediateDiscovery), \
             patch.object(camera,'CameraSession',return_value=fake), \
             patch.object(camera,'MenuState') as menus, \
             patch.object(camera.time,'monotonic',side_effect=lambda:frame[0]*.05), \
             patch.object(camera.time,'sleep'),contextlib.redirect_stdout(io.StringIO()) as output:
            menus.return_value.reason.return_value=None
            camera.interactive(game,args)
        self.assertEqual(output.getvalue().count('L3 quick turn: 180 degrees.'),1)
        self.assertAlmostEqual(fake.yaw,-10.)
        progress=[(yaw-170.)%360. for yaw,_ in views]
        self.assertTrue(any(0.<angle<180. for angle in progress))
        self.assertEqual(progress,sorted(progress))
        self.assertTrue(all(pitch==-35. for _,pitch in views))
        fake.animate_out.assert_not_called()
        fake.enable_controller_profile.assert_called_once()
        fake.restore.assert_called_once()
        fake.tune.assert_not_called()

    def test_reconnect_opens_new_process_and_does_not_auto_enable(self):
        args = SimpleNamespace(start_enabled=True)
        seen = []
        inputs = Mock()
        inputs.pressed.return_value = False
        def run(game, options, stop_event=None):
            seen.append(options.start_enabled)
            return 'game_closed' if len(seen) == 1 else 'quit'
        with patch.object(camera, 'NativeInput', return_value=inputs), \
             patch.object(camera, 'find_game_pid', side_effect=[11, 22]), \
             patch.object(camera, 'CameraMemory') as memory, \
             patch.object(camera, 'interactive', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            camera.run_live(args)
        self.assertEqual([call.args[0] for call in memory.call_args_list], [11, 22])
        self.assertEqual(seen, [True, False])

    def test_controller_look_does_not_warp_mouse(self):
        inputs = camera.NativeInput.__new__(camera.NativeInput)
        inputs.user = Mock()
        inputs.controller_activity = True
        inputs.using_gamepad = False
        inputs.last_mouse = None
        self.assertEqual(inputs.mouse(123), (0, 0))
        inputs.user.SetCursorPos.assert_not_called()

    def test_shoulder_key_is_edge_triggered_without_global_hotkey_capture(self):
        inputs = camera.NativeInput.__new__(camera.NativeInput)
        inputs.user, inputs.hotkeys, inputs.previous = Mock(), {}, {}
        inputs.user.GetAsyncKeyState.side_effect = [0, 0x8000, 0x8000, 0, 0x8000]
        self.assertEqual([inputs.pressed(0x71) for _ in range(5)], [False, True, False, False, True])
        inputs.user.RegisterHotKey.assert_not_called()

    def test_keyboard_dodge_release_is_observed_even_with_controller_activity(self):
        inputs = camera.NativeInput.__new__(camera.NativeInput)
        inputs.user, inputs.controller_activity = Mock(), True
        inputs.user.GetAsyncKeyState.side_effect = [0x8000, 0]
        self.assertTrue(inputs.keyboard_dodge_down())
        self.assertFalse(inputs.keyboard_dodge_down())
        self.assertEqual([call.args for call in inputs.user.GetAsyncKeyState.call_args_list], [(0x52,), (0x52,)])

    def test_held_r_does_not_rearm_dodge_facing_when_controller_activity_changes(self):
        from locomotion_facing import DodgeFacingIntent
        from roll_direction import DODGE_BUTTONS
        inputs = camera.NativeInput.__new__(camera.NativeInput)
        inputs.user = Mock()
        inputs.user.GetAsyncKeyState.return_value = 0
        intent = DodgeFacingIntent(DODGE_BUTTONS)
        intent.sample(inputs.roll_buttons(0),20,(0,-1),0,eligible=True)
        inputs.user.GetAsyncKeyState.return_value = 0x8000
        for now,pad in ((.01,False),(.20,True),(.22,False)):
            inputs.controller_activity=pad
            intent.sample(inputs.roll_buttons(0),20,(0,-1),now,eligible=True)
            if now==.01:self.assertEqual(intent.pending(now),-160)
            else:self.assertIsNone(intent.pending(now))

    def test_full_stick_travel_stays_normalized_for_dodge_validation(self):
        class Pad(C.Structure):
            _fields_=[('buttons',C.c_ushort),('leftTrigger',C.c_ubyte),('rightTrigger',C.c_ubyte),
                      ('leftX',C.c_short),('leftY',C.c_short),('rightX',C.c_short),('rightY',C.c_short)]
        class State(C.Structure):
            _fields_=[('packet',C.c_uint32),('pad',Pad)]
        inputs=camera.NativeInput.__new__(camera.NativeInput)
        inputs.State=State;inputs.xinput=Mock()
        for endpoint in (-32768,32767):
            def state(index,out):
                out._obj.pad=Pad(0x80,0,0,endpoint,endpoint,endpoint,endpoint)
                return 0
            inputs.xinput.XInputGetState.side_effect=state
            x,y,buttons=inputs.gamepad()
            expected=-1. if endpoint<0 else 1.
            self.assertEqual(inputs.left_stick,(expected,expected))
            self.assertEqual((x,y,buttons),(expected,expected,0x80))
            self.assertFalse(inputs.gamepad_attack_held)
        for buttons,trigger,expected in ((0x40,0,False),(0x80,0,False),(0x4000,0,True),(0,31,True)):
            def state(index,out,b=buttons,t=trigger):
                out._obj.pad=Pad(b,0,t,0,0,0,0)
                return 0
            inputs.xinput.XInputGetState.side_effect=state
            inputs.gamepad()
            self.assertIs(inputs.gamepad_attack_held,expected)
        inputs.xinput.XInputGetState.return_value=1167
        inputs.xinput.XInputGetState.side_effect=None
        self.assertEqual(inputs.gamepad(),(0,0,0))
        self.assertEqual(inputs.left_stick,(0.,0.))
        self.assertIsNone(inputs.gamepad_index)
        self.assertFalse(inputs.gamepad_attack_held)

    def test_attack_intent_respects_focus_and_input_method_before_ability_start(self):
        inputs=camera.NativeInput.__new__(camera.NativeInput)
        inputs.user=Mock()
        inputs.controller_activity=True
        inputs.gamepad_attack_held=True
        self.assertTrue(inputs.attack_held(123))
        self.assertFalse(inputs.attack_held(None))
        inputs.user.GetAsyncKeyState.assert_not_called()
        inputs.gamepad_attack_held=False
        self.assertFalse(inputs.attack_held(123))
        inputs.controller_activity=False
        for key in (1,2,0x31,0x32,0x33,0x45):
            inputs.user.GetAsyncKeyState.side_effect=lambda vk,k=key:0x8000 if vk==k else 0
            self.assertTrue(inputs.attack_held(123))
        inputs.user.GetAsyncKeyState.side_effect=lambda vk:0x8000 if vk==0x52 else 0
        self.assertFalse(inputs.attack_held(123))  # R must not recenter a backward dodge.

    def test_world_rotation_is_restored_after_configuration(self):
        session = self.make_session()
        session.originals = {120: [100, b'length', None], 130: [100, b'rotation', None], 140: [100, b'quaternion', None]}
        session.game.read.side_effect = lambda address, size: session.originals[address][1]
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        self.assertEqual([c.args[0] for c in session.game.write.call_args_list], [120, 130, 140])
        self.assertFalse(session.restore_failures)

    def test_native_instruction_restoration_precedes_normal_fields_and_is_retryable(self):
        session = self.make_session()
        events = []
        manager = session.game.native_roll_hook = Mock(restore_failures={'busy'})
        manager.restore.side_effect = lambda: events.append('native')
        session.game.read.return_value = b'\x01'
        session.game.write.side_effect = lambda *args: events.append('field')
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        self.assertEqual(events, ['native', 'field'])
        self.assertIn('native roll instructions', session.restore_failures)
        manager.restore_failures.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            session.restore()
        self.assertFalse(session.restore_failures)

    def test_native_prototype_rejects_incompatible_options_before_opening_game(self):
        for extra in ([], ['--interactive','--native-combat'],
                      ['--interactive','--no-wasd'], ['--interactive','--native-roll-direction']):
            with self.subTest(extra=extra), patch.object(camera, 'CameraMemory') as memory, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                camera.main(['--native-roll-hook', *extra])
            memory.assert_not_called()

    def test_native_profile_is_opt_in_and_disables_only_the_old_roll_force_editor(self):
        fake = Mock(yaw=0, pitch=0, blend=1., blend_target=1., restore_failures=set())
        inputs, game = Mock(), Mock()
        frame = [0]
        inputs.pump.side_effect = lambda: frame.__setitem__(0, frame[0]+1)
        inputs.pressed.side_effect = lambda key: key == 0x79 and frame[0] == 3
        inputs.foreground.side_effect = [123, None, 123]
        inputs.gamepad.return_value = (0,0,0)
        inputs.mouse.return_value = (0,0)
        def alive(handle, code):
            code._obj.value=259; return True
        game.kernel.GetExitCodeProcess.side_effect = alive
        args = SimpleNamespace(seconds=0, start_enabled=True, height=55, fov=115,
                               sensitivity=.12, native_roll_hook=True)
        with patch.object(camera, 'NativeInput', return_value=inputs), \
             patch.object(camera, 'CameraDiscovery', ImmediateDiscovery), \
             patch.object(camera, 'CameraSession', return_value=fake), \
             patch.object(camera, 'MenuState') as menus, \
             patch.object(camera.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            menus.return_value.reason.return_value = None
            camera.interactive(game, args)
        fake.enable_combat_profile.assert_called_once_with(steer_roll=False, filter_ranged=False)
        fake.enable_native_roll_profile.assert_called_once()
        self.assertEqual([c.args[0] for c in fake.native_roll_profile.set_input.call_args_list], [True, False])
        fake.enable_controller_profile.assert_called_once()
        fake.enable_keyboard_movement.assert_called_once()
        fake.enable_mouse_bindings.assert_called_once()
        fake.hide_cursor.assert_called_once()


if __name__ == '__main__':
    unittest.main()
