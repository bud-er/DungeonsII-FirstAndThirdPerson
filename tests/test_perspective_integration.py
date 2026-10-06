import contextlib
import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import external_camera as camera
from camera_modes import Perspective
from launcher_settings import DEFAULTS, helper_arguments, validate_settings
import test_launcher
import test_camera_pose
import test_external_camera


class PerspectiveIntegration(unittest.TestCase):
    def test_stick_roll_guard_survives_menu_and_resume_in_both_perspectives(self):
        from test_controller_profile import ControllerAxisBlocking
        for third in (False, True):
            with self.subTest(third_person=third):
                s,_=test_external_camera.ExternalCameraRestoration().ui_session()
                profile,memory=ControllerAxisBlocking().profile()
                s.controller_profile=profile
                s.pc,s.pawn,s.camera=100,101,102
                s.pointer=lambda obj,cls,name:101 if name=='Pawn' else 102
                s.blend,s.blend_target,s.blend_started=1.,1.,None
                s.pose,s.avatar_profile,s.silhouette_profile=Mock(),None,None
                s.perspective=Perspective(third=third)
                s.update()
                self.assertEqual(memory.read(1000+0x28,8),bytes(8))
                self.assertEqual(struct.unpack('<I',memory.read(1080+0x28,4))[0],3)
                with contextlib.redirect_stdout(io.StringIO()):s.set_ui_input(True)
                # Simulate a native key reset while the menu is displayed.
                memory.put(1000+0x28,profile.axis_key)
                s.update()
                self.assertEqual(memory.read(1000+0x28,8),bytes(8))
                self.assertEqual(struct.unpack('<I',memory.read(1080+0x28,4))[0],2)
                with contextlib.redirect_stdout(io.StringIO()):s.set_ui_input(False)
                s.update()
                self.assertEqual(memory.read(1000+0x28,8),bytes(8))
                self.assertEqual(struct.unpack('<I',memory.read(1080+0x28,4))[0],3)
                self.assertEqual(s.perspective.target,float(third))

    def test_shoulder_pose_follows_camera_bearing_and_leaves_zoom_arm_intact(self):
        s, g = test_camera_pose.ZoneCamera().make_session()
        s.perspective = Perspective(third=True)
        arm = g.read(s.arm, 0x360)
        s.yaw, s.pitch = 90., 0.
        s.update()
        actual = test_camera_pose.ZoneCamera.rendered_position(s)
        for a, b in zip(actual, (55., -20., 365.)):
            self.assertAlmostEqual(a, b)
        self.assertEqual(g.read(s.arm, 0x360), arm)
        self.assertEqual(struct.unpack('<f', g.read(s.pose.additive_fov, 4)), (45.,))

    def test_full_body_returns_in_third_person_and_first_person_rehides_it(self):
        s, g = test_camera_pose.ZoneCamera().make_session()
        s.avatar_profile = Mock(applied=True, restore_failures=set())
        s.perspective = Perspective(third=True)
        s.update()
        s.avatar_profile.restore.assert_called_once()
        s.avatar_profile.update.assert_not_called()
        s.perspective = Perspective()
        s.update()
        s.avatar_profile.update.assert_called_once()

    def test_menu_restores_sprint_before_input_handoff(self):
        s, _ = test_external_camera.ExternalCameraRestoration().ui_session()
        order = []
        s.sprint_profile = Mock(restore_failures=set())
        s.sprint_profile.restore.side_effect = lambda: order.append('sprint')
        s.controller_profile.set_ui_input.side_effect = lambda captured: order.append('input')
        with contextlib.redirect_stdout(io.StringIO()): s.set_ui_input(True)
        self.assertEqual(order, ['sprint', 'input'])

    def test_inventory_hidden_armor_survives_camera_fade_to_native(self):
        s,_=test_camera_pose.ZoneCamera().make_session()
        s.hidden_armor_slots={'helmet'}
        s.avatar_profile=Mock(applied=False,restore_failures=set())
        s.avatar_profile.equipment=Mock(shared=True,restore_failures=set())
        for blend in (.79,.5,0.):
            s.blend=blend
            s.update()
        self.assertEqual(s.avatar_profile.equipment.update.call_count,3)
        s.avatar_profile.equipment.update.assert_called_with(hide_body=False)
        s.avatar_profile.equipment.restore.assert_not_called()

    def test_experimental_view_sync_is_disabled_without_visible_arms(self):
        s,_=test_camera_pose.ZoneCamera().make_session()
        s.sync_first_person_view=True;s.blend_target=1.
        s.dialogue_active=False;s.input_paused=False;s.view_target_profile=Mock()
        s.update()
        s.view_target_profile.sync_heading.assert_called_once_with(False)

    def test_sprint_setup_mismatch_keeps_camera_controls_available(self):
        fake = Mock(yaw=20., pitch=-10., blend=1., blend_target=1., restore_failures=set(),
                    damage_feedback=None, sprint_profile=None)
        fake.enable_sprint.side_effect = RuntimeError('Native parameter guard mismatch')
        inputs, game, frame = Mock(), Mock(), [0]
        inputs.gamepad_index = 0
        inputs.pump.side_effect = lambda: frame.__setitem__(0,frame[0]+1)
        inputs.pressed.side_effect = lambda key: key == 0x79 and frame[0] == 5
        inputs.foreground.return_value = 123
        inputs.mouse.return_value = (0,0)
        inputs.gamepad.return_value = (0,0,0)
        inputs.interact_modifier.return_value = False
        inputs.attack_held.return_value = False
        def alive(handle,code):
            code._obj.value=259
            return True
        game.kernel.GetExitCodeProcess.side_effect=alive
        args=SimpleNamespace(seconds=0,start_enabled=True,height=55,fov=115,sensitivity=.12,
            sprint=True,sprint_multiplier=1.3,no_avatar=True,no_damage_flash=True,
            keep_mob_silhouettes=True)
        with patch.object(camera,'NativeInput',return_value=inputs), \
             patch.object(camera,'CameraDiscovery',test_external_camera.ImmediateDiscovery), \
             patch.object(camera,'CameraSession',return_value=fake), \
             patch.object(camera,'MenuState') as menus, \
             patch.object(camera.time,'sleep'), contextlib.redirect_stdout(io.StringIO()) as out:
            menus.return_value.reason.return_value=None
            camera.interactive(game,args)
        fake.enable_sprint.assert_called_once_with(1.3)
        fake.enable_combat_profile.assert_called_once()
        fake.transition_to.assert_called_once_with(1.,start=0.)
        self.assertGreater(fake.update.call_count,0)
        self.assertIn('Sprint unavailable:',out.getvalue())
        self.assertNotIn('Camera suspended',out.getvalue())

    def test_invalid_preview_arguments_fail_before_opening_game(self):
        for argv in (['--sprint', '--no-wasd'], ['--shoulder-distance', '0'],
                     ['--sprint-multiplier', '99'], ['--transition-duration', 'nan']):
            with patch.object(camera, 'CameraMemory') as memory, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): camera.main(argv)
            memory.assert_not_called()

    def test_new_settings_and_feature_switches(self):
        self.assertFalse(DEFAULTS['sprint'])
        args = helper_arguments(dict(DEFAULTS, sprint=True, third_person=False,
            quick_turn=False, keyboard_gestures=False, direction_sprint=False, shoulder_side=-40))
        for flag in ('--sprint', '--no-third-person', '--no-quick-turn',
                     '--no-keyboard-gestures', '--no-direction-sprint'):
            self.assertIn(flag, args)
        self.assertEqual(float(args[args.index('--shoulder-side')+1]), -40.)
        with self.assertRaises(ValueError): helper_arguments(dict(DEFAULTS, sprint=True, first_person_controls=False))
        for key, value in (('shoulder_side', 121), ('shoulder_height', 0), ('transition_duration', 2)):
            with self.assertRaises(ValueError): validate_settings(dict(DEFAULTS, **{key: value}))

    def test_clipboard_report_contains_no_private_paths_and_no_auto_copy(self):
        app = test_launcher.CompatibilityUI().app()
        app.copy_compatibility_report()
        app.root.clipboard_append.assert_not_called()
        app.compatibility_result = {'state': 'unsupported', 'source': 'selected', 'copies': [
            {'executable': 'C:/Users/PrivateName/Game/Dungeons-WinGDK-Shipping.exe', 'sha256': 'a'*64}]}
        app.compatibility_busy = True
        app.copy_compatibility_report()
        app.root.clipboard_append.assert_not_called()
        app.compatibility_busy = False
        app.copy_compatibility_report()
        report = app.root.clipboard_append.call_args.args[0]
        json.loads(report)
        self.assertIn('a'*64, report)
        self.assertNotIn('PrivateName', report)
        self.assertNotIn('C:/', report)

    def test_camera_gestures_are_exclusive_and_can_be_disabled(self):
        for gesture, enabled, starts_on in (('f3', True, True), ('triple', True, True),
                                           ('f3', False, True), ('f3', True, False)):
            with self.subTest(gesture=gesture, enabled=enabled, starts_on=starts_on):
                fake = Mock(yaw=20., pitch=-10., blend=1., blend_target=1., restore_failures=set(),
                            damage_feedback=None, sprint_profile=None)
                inputs, game, frame = Mock(), Mock(), [0]
                inputs.gamepad_index = 0
                inputs.pump.side_effect = lambda: frame.__setitem__(0, frame[0]+1)
                inputs.pressed.side_effect = lambda key: (key == 0x79 and frame[0] == 20) or \
                    (key == 0x72 and frame[0] == 7 and gesture == 'f3')
                inputs.foreground.return_value = 123
                inputs.mouse.return_value = (0, 0)
                inputs.interact_modifier.return_value = False
                inputs.attack_held.return_value = False
                inputs.gamepad.side_effect = lambda: (0, 0, 0x40 if gesture == 'triple' and frame[0] in (2,4,6) else 0)
                def alive(handle, code):
                    code._obj.value = 259
                    return True
                game.kernel.GetExitCodeProcess.side_effect = alive
                args = SimpleNamespace(seconds=0, start_enabled=starts_on, height=55, fov=115, sensitivity=.12,
                    no_avatar=True, no_damage_flash=True, keep_mob_silhouettes=True, no_third_person=not enabled)
                with patch.object(camera, 'NativeInput', return_value=inputs), \
                     patch.object(camera, 'CameraDiscovery', test_external_camera.ImmediateDiscovery), \
                     patch.object(camera, 'CameraSession', return_value=fake), \
                     patch.object(camera, 'MenuState') as menus, \
                     patch.object(camera.time, 'monotonic', side_effect=lambda: frame[0]*.05), \
                     patch.object(camera.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
                    menus.return_value.reason.return_value = None
                    camera.interactive(game, args)
                if enabled and starts_on:
                    fake.perspective.select.assert_called_once()
                    self.assertIs(fake.perspective.select.call_args.args[0], True)
                    self.assertIn('Third person ON.', output.getvalue())
                else:
                    fake.perspective.select.assert_not_called()
                self.assertEqual(fake.yaw, 20.)
                fake.animate_out.assert_not_called()

    def test_f2_swaps_only_during_focused_third_person_gameplay_and_remembers_side(self):
        for condition in ('enabled', 'disabled', 'first', 'unfocused', 'menu'):
            with self.subTest(condition=condition):
                fake = Mock(yaw=20., pitch=-10., blend=1., blend_target=1., restore_failures=set(),
                            damage_feedback=None, sprint_profile=None)
                fake.perspective = Perspective(side=75)
                inputs, game, frame = Mock(), Mock(), [0]
                inputs.gamepad_index = 0
                inputs.pump.side_effect = lambda: frame.__setitem__(0, frame[0]+1)
                inputs.pressed.side_effect = lambda key: (key == 0x79 and frame[0] == 20) or \
                    (key == 0x72 and frame[0] == 3 and condition != 'first') or \
                    (key == 0x71 and frame[0] == 7)
                # foreground is read before pump advances this frame.
                inputs.foreground.side_effect = lambda: None if condition == 'unfocused' and frame[0] == 6 else 123
                inputs.mouse.return_value = (0, 0)
                inputs.interact_modifier.return_value = False
                inputs.attack_held.return_value = False
                inputs.gamepad.return_value = (0, 0, 0)
                def alive(handle, code):
                    code._obj.value = 259
                    return True
                game.kernel.GetExitCodeProcess.side_effect = alive
                args = SimpleNamespace(seconds=0, start_enabled=True, height=55, fov=115, sensitivity=.12,
                    no_avatar=True, no_damage_flash=True, keep_mob_silhouettes=True,
                    no_shoulder_switch=condition == 'disabled', shoulder_side=75.)
                with patch.object(camera, 'NativeInput', return_value=inputs), \
                     patch.object(camera, 'CameraDiscovery', test_external_camera.ImmediateDiscovery), \
                     patch.object(camera, 'CameraSession', return_value=fake), \
                     patch.object(camera, 'MenuState') as menus, \
                     patch.object(camera.time, 'monotonic', side_effect=lambda: frame[0]*.05), \
                     patch.object(camera.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                    def menu_reason():
                        menus.return_value.input_captured = condition == 'menu' and frame[0] >= 6
                        menus.return_value.names = ['W_Enchantsmith_Activatable'] if menus.return_value.input_captured else []
                        return None
                    menus.return_value.reason.side_effect = menu_reason
                    camera.interactive(game, args)
                self.assertEqual(args.shoulder_side, -75 if condition == 'enabled' else 75)
                self.assertEqual(fake.yaw, 20.)
                fake.animate_out.assert_not_called()


if __name__ == '__main__': unittest.main()
