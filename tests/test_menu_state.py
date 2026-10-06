import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import external_camera as camera
from menu_state import captures_menu, LocalUICapture, MenuState, keeps_first_person
from local_menu_scope import MenuPresentationChanging
from test_external_camera import ImmediateDiscovery


class MenuDetection(unittest.TestCase):
    def monitor(self, names):
        monitor = MenuState.__new__(MenuState)
        s = monitor.session = SimpleNamespace(pc=1, pawn=2, camera=3, identities={1:'pc', 2:'pawn'})
        s.identity = lambda obj: {1:'pc', 2:'pawn'}.get(obj, 'widget')
        s.object_retired = lambda obj, identity: False
        s.pointer = lambda obj, cls, field: 2 if field == 'Pawn' else 3
        monitor.game = Mock()
        monitor.game.read.side_effect = lambda address, size: bytes(({100:1, 200:3, 300:4}[address],))
        monitor.widgets = {10+i:('widget', name, 100, 1, 200, 300) for i,name in enumerate(names)}
        monitor.next_read, monitor.names = 0, ()
        monitor.advance_discovery = Mock()
        monitor.local_capture = Mock(captured=Mock(return_value=True))
        monitor.local_scope = Mock(begin=Mock(return_value=()), presented=Mock(return_value=True))
        return monitor

    def test_small_menus_capture_input_without_releasing_the_view(self):
        for name in ('MiniInventory', 'W_QuestDialogue_Activatable', 'W_QuestAcceptance_Activatable'):
            with self.subTest(name=name):
                monitor = self.monitor([name+'_C_23'])
                self.assertIsNone(monitor.reason())
                self.assertTrue(monitor.input_captured)
                self.assertTrue(keeps_first_person(name+'_C_23'))

    def test_full_menu_overrides_a_retained_small_menu(self):
        monitor = self.monitor(['MiniInventory_C_1', 'Inventory_C_2'])
        self.assertEqual(monitor.reason(), 'menu')
        self.assertTrue(monitor.input_captured)

    def test_real_camera_switch_still_releases_the_view(self):
        monitor = self.monitor(['MiniInventory_C_1'])
        monitor.session.pointer = lambda obj, cls, field: 2 if field == 'Pawn' else 4
        self.assertEqual(monitor.reason(), 'game camera')

    def test_native_ui_handoff_does_not_wait_for_widget_discovery(self):
        monitor = self.monitor([])
        self.assertIsNone(monitor.reason())
        self.assertTrue(monitor.input_captured)
        self.assertEqual(monitor.names, ())
        monitor.game.read.assert_not_called()

    def test_native_ui_handoff_does_not_require_menu_only_input_mode(self):
        monitor = self.monitor(['InputOverlay_C_1'])
        monitor.game.read.side_effect = lambda address, size: bytes(({100:1, 200:1, 300:4}[address],))
        self.assertIsNone(monitor.reason())
        self.assertTrue(monitor.input_captured)
        self.assertEqual(monitor.names, ())

    def test_retiring_widget_cannot_return_input_owned_by_native_ui(self):
        monitor = self.monitor(['Inventory_C_1'])
        monitor.game.read.side_effect = OSError('widget retired during read')
        monitor.session.object_retired = lambda obj, identity: True
        self.assertIsNone(monitor.reason())
        self.assertTrue(monitor.input_captured)
        self.assertEqual(monitor.names, ())
        self.assertFalse(monitor.widgets)

    def test_ui_capture_is_read_between_throttled_widget_reads(self):
        monitor = self.monitor([])
        monitor.input_captured = False
        monitor.next_read = 100.
        with patch('menu_state.time.monotonic', return_value=1.):
            self.assertIsNone(monitor.reason())
        self.assertTrue(monitor.input_captured)
        monitor.local_capture.captured.assert_called_once_with(monitor.session.pc)

    def test_native_ui_release_does_not_override_a_presented_full_menu(self):
        monitor = self.monitor(['Inventory_C_1'])
        with patch('menu_state.time.monotonic', return_value=1.):
            self.assertEqual(monitor.reason(), 'menu')
            monitor.local_capture.captured.return_value = False
            self.assertEqual(monitor.reason(), 'menu')
        self.assertTrue(monitor.input_captured)

    def test_local_inventory_captures_input_when_native_capture_remains_false(self):
        monitor = self.monitor(['W_PlayerInventory_Activatable_C_1'])
        monitor.local_capture.captured.return_value = False
        self.assertEqual(monitor.reason(), 'menu')
        self.assertTrue(monitor.input_captured)

    def test_presentation_transition_releases_input_without_camera_recovery(self):
        monitor = self.monitor([])
        monitor.local_capture.captured.return_value = False
        monitor.local_scope.begin.side_effect = MenuPresentationChanging('stack animation')
        self.assertIsNone(monitor.reason())
        self.assertTrue(monitor.input_captured)
        monitor.local_scope.begin.side_effect = None
        self.assertIsNone(monitor.reason())
        self.assertFalse(monitor.input_captured)

    def test_unknown_live_widget_read_never_recaptures_input(self):
        monitor = self.monitor(['Inventory_C_1'])
        monitor.game.read.side_effect = OSError('unreadable live widget')
        with self.assertRaises(OSError):
            monitor.reason()

    def test_unreadable_native_capture_is_not_hidden_by_widget_cache(self):
        monitor = self.monitor(['MiniInventory_C_1'])
        with patch('menu_state.time.monotonic', return_value=1.):
            self.assertIsNone(monitor.reason())
            monitor.local_capture.captured.side_effect = OSError('unreadable controller')
            with self.assertRaises(OSError):
                monitor.reason()
        self.assertTrue(monitor.input_captured)

    def test_only_active_visible_menu_widgets_capture_input(self):
        self.assertTrue(captures_menu(True, 3, 4))
        self.assertFalse(captures_menu(False, 3, 4))
        for mode in (0, 1, 2):
            self.assertFalse(captures_menu(True, mode, 4))
        for visibility in (1, 2):
            self.assertFalse(captures_menu(True, 3, visibility))

    def test_removed_widgets_do_not_permanently_block_gameplay(self):
        monitor = MenuState.__new__(MenuState)
        s = monitor.session = SimpleNamespace(pc=1, pawn=2, camera=3, identities={1:'pc',2:'pawn'})
        s.identity = Mock(side_effect=lambda obj: {1:'pc',2:'pawn'}[obj] if obj in (1,2) else (_ for _ in ()).throw(RuntimeError('retired')))
        s.pointer = Mock(side_effect=lambda obj, cls, field: 2 if field == 'Pawn' else 3)
        s.object_retired = lambda obj, identity: True
        monitor.game = Mock()
        monitor.widgets = {4:('old','inventory',100,1,200,300)}
        monitor.next_read, monitor.names = 0, ('inventory',)
        monitor.advance_discovery = Mock()
        monitor.local_capture = Mock(captured=Mock(return_value=True))
        monitor.local_scope = Mock(begin=Mock(return_value=()), presented=Mock(return_value=True))
        self.assertIsNone(monitor.reason())
        self.assertFalse(monitor.widgets)
        monitor.game.read.assert_not_called()


    def test_surviving_world_map_cannot_pause_after_local_input_returns(self):
        monitor = self.monitor(['W_WorldMap_Activatable_C_1'])
        self.assertEqual(monitor.reason(), 'menu')
        # Its allocation, active bit, input mode and visibility remain valid.
        # The local controller has nevertheless returned input to gameplay.
        monitor.local_capture.captured.return_value = False
        monitor.local_scope.presented.return_value = False  # Removed from its active stack.
        monitor.next_read = 0
        self.assertIsNone(monitor.reason())
        self.assertFalse(monitor.input_captured)
        self.assertEqual(monitor.names, ())
        self.assertTrue(monitor.widgets)  # Keep it discoverable for reuse.
        monitor.local_capture.captured.assert_called_with(monitor.session.pc)

        # Reusing that same map object for a real open menu must still pause.
        monitor.local_capture.captured.return_value = True
        monitor.local_scope.presented.return_value = True
        monitor.next_read = 0
        self.assertEqual(monitor.reason(), 'menu')
        self.assertTrue(monitor.input_captured)

    def test_a_real_menu_does_not_expire_while_local_input_remains_captured(self):
        monitor = self.monitor(['W_WorldMap_Activatable_C_1'])
        for now in (1, 120, 600, 3600):
            with patch('menu_state.time.monotonic', return_value=now):
                self.assertEqual(monitor.reason(), 'menu')

    def test_capture_failure_never_means_menu_closed(self):
        monitor = self.monitor(['W_WorldMap_Activatable_C_1'])
        self.assertEqual(monitor.reason(), 'menu')
        monitor.next_read = 0
        monitor.local_capture.captured.side_effect = OSError('unreadable')
        with self.assertRaises(OSError):
            monitor.reason()
        self.assertTrue(monitor.input_captured)
        self.assertTrue(monitor.names)

    def test_game_camera_switch_is_not_ignored_when_native_ui_is_clear(self):
        monitor = self.monitor(['W_WorldMap_Activatable_C_1'])
        monitor.local_capture.captured.return_value = False
        monitor.local_scope.presented.return_value = False
        monitor.session.pointer = lambda obj, cls, field: 2 if field == 'Pawn' else 4
        self.assertEqual(monitor.reason(), 'game camera')


class LocalCaptureRead(unittest.TestCase):
    def game(self, value=0):
        game = Mock(base=0x10000000)
        game.read.side_effect = lambda address, size: (
            LocalUICapture.GETTER_BYTES if address == game.base+LocalUICapture.GETTER_RVA
            else bytes([value]))
        return game

    def test_verified_native_getter_reads_only_local_controller_boolean(self):
        for value in (0, 1):
            game = self.game(value)
            capture = LocalUICapture(game)
            self.assertEqual(capture.captured(0x20000000), bool(value))
            self.assertEqual(game.read.call_args_list[-1].args, (0x20000984, 1))
            game.write.assert_not_called()

    def test_unsupported_getter_is_not_guessed(self):
        game = Mock(base=0x10000000)
        game.read.return_value = b'\x90'*8
        with self.assertRaisesRegex(RuntimeError, 'Unsupported local UI'):
            LocalUICapture(game)

    def test_non_boolean_and_unreadable_native_state_are_not_closed(self):
        capture = LocalUICapture(self.game(2))
        with self.assertRaisesRegex(RuntimeError, 'Unknown local UI'):
            capture.captured(0x20000000)
        capture.game.read.side_effect = OSError('unreadable')
        with self.assertRaises(OSError):
            capture.captured(0x20000000)


class MenuResume(unittest.TestCase):
    def run_flow(self, menu_frames=(), toggle_frames=(), buttons=0, focus_frames=None,
                 silhouette_frames=(), keep_silhouettes=False, start_enabled=True, small_menu_frames=(),
                 native_capture_false=False):
        frame, instances, created = [0], [], []
        controls = Mock()
        controls.pump.side_effect = lambda: frame.__setitem__(0, frame[0]+1)
        controls.foreground.side_effect = lambda: None if focus_frames and frame[0]+1 in focus_frames else 123
        controls.pressed.side_effect = lambda key: (key == 0x79 and frame[0] == 14) or (key == 0x75 and frame[0] in toggle_frames) or (key == 0x73 and frame[0] in silhouette_frames)
        look_frames, look_calls = [], []
        def mouse(*args, **kwargs):
            look_frames.append(frame[0])
            look_calls.append((frame[0], kwargs.get('discard', False)))
            return (0,0)
        controls.mouse.side_effect = mouse
        controls.gamepad.return_value = (0,0,buttons)
        game = Mock()
        def alive(handle, code):
            code._obj.value = 259
            return True
        game.kernel.GetExitCodeProcess.side_effect = alive
        def session(*args, **kwargs):
            result = Mock(pawn=42, pc=100, yaw=75., pitch=-20., blend=1., blend_target=1., restore_failures=set(), damage_feedback=None, input_paused=False)
            result.update_frames = []
            result.look_frames = look_frames
            result.look_calls = look_calls
            result.set_ui_input.side_effect = lambda captured:setattr(result, 'input_paused', captured)
            result.update.side_effect = lambda: result.update_frames.append(frame[0])
            # A read-only warmup session never becomes an active camera.
            result.enable_controller_profile.side_effect = lambda:instances.append(result)
            created.append(result)
            return result
        args = SimpleNamespace(seconds=0, start_enabled=start_enabled, height=55, fov=115, sensitivity=.12,
                               no_avatar=True, no_damage_flash=True, keep_mob_silhouettes=keep_silhouettes)
        with patch.object(camera, 'NativeInput', return_value=controls), \
             patch.object(camera, 'CameraDiscovery', ImmediateDiscovery), \
             patch.object(camera, 'CameraSession', side_effect=session) as factory, \
             patch.object(camera, 'MenuState') as menu, \
             patch.object(camera.time, 'monotonic', side_effect=lambda: frame[0]*.1), \
             patch.object(camera.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            def menu_factory(session):
                menu.return_value.session = session
                return menu.return_value
            menu.side_effect = menu_factory
            menu.return_value.names = ('Inventory',)
            def reason():
                if native_capture_false:
                    names = ['MiniInventory_C_1'] if frame[0] in small_menu_frames else \
                        ['Inventory_C_1'] if frame[0] in menu_frames else []
                    detector = MenuDetection().monitor(names)
                    detector.local_capture.captured.return_value = False
                    result = detector.reason()
                    menu.return_value.input_captured = detector.input_captured
                    menu.return_value.names = detector.names
                    return result
                menu.return_value.input_captured = frame[0] in small_menu_frames or frame[0] in menu_frames
                return 'menu' if frame[0] in menu_frames else None
            menu.return_value.reason.side_effect = reason
            camera.interactive(game, args)
        factory.created = created
        return instances, factory

    def test_read_only_prewarm_never_activates_or_restores_camera(self):
        active,factory=self.run_flow(start_enabled=False)
        self.assertEqual(active,[])
        self.assertEqual(len(factory.created),1)
        seed=factory.created[0]
        seed.update.assert_not_called();seed.restore.assert_not_called()
        seed.enable_controller_profile.assert_not_called()

    def test_each_toggle_captures_fresh_baselines_using_cached_discovery(self):
        active,factory=self.run_flow(start_enabled=False,toggle_frames=(3,6,9))
        self.assertEqual(len(active),2)
        self.assertEqual(len(factory.created),3)  # One warmup, two fresh active sessions.
        self.assertIs(factory.call_args_list[1].kwargs['previous'],factory.created[0])
        self.assertIs(factory.call_args_list[2].kwargs['previous'],active[0])
        self.assertTrue(all(s.update_frames for s in active))

    def test_inventory_close_resumes_without_f6_and_preserves_look(self):
        instances, factory = self.run_flow(menu_frames=range(3,7))
        self.assertEqual(len(instances), 2)
        first, resumed = instances
        self.assertEqual(first.update_frames, [1,2])
        self.assertGreaterEqual(min(resumed.update_frames), 8)
        self.assertEqual((resumed.yaw,resumed.pitch), (75.,-20.))
        first.restore.assert_called_once()
        resumed.restore.assert_called_once()
        self.assertIs(factory.call_args.kwargs['previous'], first)

    def test_small_menu_keeps_the_same_camera_and_stops_look_and_gameplay_input(self):
        active, factory = self.run_flow(small_menu_frames=range(3, 7))
        self.assertEqual(len(active), 1)
        view = active[0]
        self.assertEqual(view.update_frames, list(range(1, 14)))
        self.assertFalse(set(range(3, 7)) & set(view.look_frames))
        self.assertEqual((view.yaw, view.pitch), (75., -20.))
        view.restore.assert_called_once()  # Only when the helper exits.
        eligibility = [call.kwargs['eligible'] for call in view.keyboard_movement.set_input.call_args_list]
        self.assertEqual(eligibility[2:6], [False]*4)
        self.assertEqual(eligibility[6:], [True]*7)
        self.assertEqual(len(factory.created), 2)  # Warmup plus one active view.

    def test_native_false_inventory_stops_mouse_warp_and_resumes_with_discard(self):
        active, _ = self.run_flow(menu_frames=range(3, 7), native_capture_false=True)
        self.assertEqual(len(active), 2)
        self.assertFalse(set(range(3, 7)) & set(active[0].look_frames))
        self.assertEqual(active[0].update_frames, [1, 2])
        after_close = [(frame, discard) for frame, discard in active[1].look_calls if frame >= 7]
        self.assertTrue(after_close[0][1])

    def test_native_false_small_menu_keeps_camera_and_discards_resume_delta(self):
        active, _ = self.run_flow(small_menu_frames=range(3, 7), native_capture_false=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].update_frames, list(range(1, 14)))
        self.assertFalse(set(range(3, 7)) & set(active[0].look_frames))
        self.assertIn((7, True), active[0].look_calls)

    def test_f6_while_menu_is_open_cancels_auto_resume(self):
        instances, _ = self.run_flow(menu_frames=range(3,7), toggle_frames=(4,))
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].update_frames, [1,2])

    def test_dpad_lb_and_dodge_buttons_alone_never_switch_mode_off(self):
        # Actual widget state decides, even if these buttons are held/rebound.
        for buttons in (0x000F,0x0100,0x0080):
            instances, _ = self.run_flow(buttons=buttons)
            self.assertEqual(len(instances),1)
            self.assertEqual(instances[0].update_frames,list(range(1,14)))

    def test_alt_tab_keeps_camera_attached_but_stops_look_input(self):
        instances, _ = self.run_flow(focus_frames=range(3,7))
        self.assertEqual(len(instances),1)
        self.assertEqual(instances[0].update_frames,list(range(1,14)))
        self.assertFalse(set(range(3,7)) & set(instances[0].look_frames))

    def test_real_menu_still_pauses_while_alt_tabbed(self):
        instances, _ = self.run_flow(menu_frames=range(4,6),focus_frames=range(3,7))
        self.assertEqual(len(instances),2)
        self.assertEqual(instances[0].update_frames,[1,2,3])
        self.assertGreaterEqual(min(instances[1].update_frames),7)

    def test_short_focus_interruption_never_recreates_the_camera(self):
        instances, _ = self.run_flow(focus_frames=(3,))
        self.assertEqual(len(instances),1)
        self.assertEqual(instances[0].update_frames,list(range(1,14)))
        self.assertNotIn(3,instances[0].look_frames)

    def test_silhouette_toggle_keeps_camera_active_and_survives_menu_resume(self):
        instances, _ = self.run_flow(menu_frames=range(5,8), silhouette_frames=(3,))
        self.assertEqual(len(instances),2)
        self.assertEqual([c.args[1] for c in instances[0].set_silhouette_hiding.call_args_list], [True,False])
        instances[1].set_silhouette_hiding.assert_not_called()
        self.assertEqual(instances[0].update_frames,[1,2,3,4])

    def test_silhouette_toggle_while_paused_applies_on_resume(self):
        instances, _ = self.run_flow(menu_frames=range(3,7), silhouette_frames=(4,), keep_silhouettes=True)
        instances[0].set_silhouette_hiding.assert_not_called()
        self.assertEqual([c.args[1] for c in instances[1].set_silhouette_hiding.call_args_list], [True])

    def test_silhouette_toggle_is_ignored_in_other_apps(self):
        instances, _ = self.run_flow(focus_frames=range(3,7), silhouette_frames=(4,))
        self.assertEqual(len(instances),1)
        self.assertEqual([c.args[1] for c in instances[0].set_silhouette_hiding.call_args_list], [True])

    def test_silhouette_choice_survives_f6_off_and_on(self):
        instances, _ = self.run_flow(toggle_frames=(5,8), silhouette_frames=(3,))
        self.assertEqual(len(instances),2)
        instances[1].set_silhouette_hiding.assert_not_called()


if __name__ == '__main__':
    unittest.main()
