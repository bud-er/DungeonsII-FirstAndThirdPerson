import struct
import contextlib
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from controller_profile import KeySwap, ControllerProfile


class ControllerBindingTransactions(unittest.TestCase):
    def make_swap(self, inherited=False):
        # Include nonzero cached shared-pointer data to test complete-key transfer.
        self.left = struct.pack('<QQQ', 1, 0x1111, 0x2222)
        self.right = struct.pack('<QQQ', 2, 0x3333, 0x4444)
        self.storage = {80: b'array descriptor', 100: b'A'*40, 200: b'B'*40,
                        140: self.right if inherited else self.left,
                        240: self.left if inherited else self.right}
        game = Mock()
        game.read.side_effect = lambda address, size: self.storage[address]
        game.write.side_effect = lambda address, data: self.storage.__setitem__(address, data)
        camera = Mock()
        camera.identity.return_value = ('object', 12)
        profile = SimpleNamespace(game=game, camera=camera, key_offset=40)
        first = {'address': 100, 'tag': b'A'*40, 'keyBytes': self.storage[140]}
        second = {'address': 200, 'tag': b'B'*40, 'keyBytes': self.storage[240]}
        return KeySwap(profile, 50, 80, b'array descriptor', first, second, inherited)

    def test_swap_and_restore_preserves_cached_key_ownership(self):
        swap = self.make_swap()
        self.assertTrue(swap.write(swap.desired))
        self.assertEqual((self.storage[140], self.storage[240]), (self.right, self.left))
        self.assertTrue(swap.write(swap.original))
        self.assertEqual((self.storage[140], self.storage[240]), (self.left, self.right))

    def test_rebuilt_array_is_never_written_through_old_addresses(self):
        swap = self.make_swap()
        self.storage[80] = b'new descriptor!!'
        self.assertFalse(swap.write(swap.desired))
        swap.profile.game.write.assert_not_called()

    def test_replaced_rows_in_same_buffer_are_not_written(self):
        swap = self.make_swap()
        self.storage[100] = b'C'*40
        self.assertFalse(swap.write(swap.desired))
        swap.profile.game.write.assert_not_called()

    def test_recycled_input_owner_is_not_written(self):
        swap = self.make_swap()
        swap.profile.camera.identity.return_value = ('object', 13)
        self.assertFalse(swap.write(swap.desired))
        swap.profile.game.write.assert_not_called()

    def test_runtime_clone_restores_original_bindings(self):
        swap = self.make_swap(inherited=True)
        swap.write(swap.original)
        self.assertEqual((self.storage[140], self.storage[240]), (self.left, self.right))

    def test_user_binding_change_is_preserved(self):
        swap = self.make_swap()
        self.storage[140] = b'X'*24
        with self.assertRaisesRegex(RuntimeError, 'changed outside'):
            swap.write(swap.desired)
        swap.profile.game.write.assert_not_called()

    def test_partial_write_can_be_restored(self):
        swap = self.make_swap()
        self.storage[140] = self.right
        swap.write(swap.original)
        self.assertEqual((self.storage[140], self.storage[240]), (self.left, self.right))


class ControllerRetirement(unittest.TestCase):
    def profile(self):
        profile = ControllerProfile.__new__(ControllerProfile)
        profile.game, profile.camera = Mock(), Mock()
        profile.swaps = {}
        profile.collect = Mock()
        profile.active_thresholds = {(10, 'live')}
        original = struct.pack('<f', .5)
        profile.thresholds = {(10, 'live'): (100, original), (20, 'old'): (200, original)}
        profile.camera.identity.side_effect = lambda obj: 'live' if obj == 10 else (_ for _ in ()).throw(OSError('Freed'))
        storage = {100: struct.pack('<f', 10)}
        profile.game.read.side_effect = lambda address, size: storage[address]
        profile.game.write.side_effect = lambda address, value: storage.__setitem__(address, value)
        return profile

    def test_freed_old_trigger_does_not_block_f6_after_rebuild(self):
        profile = self.profile()
        with contextlib.redirect_stdout(io.StringIO()):
            profile.restore()
        self.assertEqual(profile.restore_failures, 0)
        self.assertEqual(profile.retired_records, 1)
        profile.game.write.assert_called_once_with(100, struct.pack('<f', .5))

    def test_unreadable_active_trigger_still_reports_failure(self):
        profile = self.profile()
        profile.active_thresholds.add((20, 'old'))
        with contextlib.redirect_stdout(io.StringIO()):
            profile.restore()
        self.assertEqual(profile.restore_failures, 1)

    def test_incomplete_snapshot_cannot_silently_retire_records(self):
        profile = self.profile()
        profile.collect.side_effect = OSError('Input array unavailable')
        with contextlib.redirect_stdout(io.StringIO()):
            profile.restore()
        self.assertGreaterEqual(profile.restore_failures, 1)

    def test_recycled_retired_trigger_is_never_written(self):
        profile = self.profile()
        profile.camera.identity.side_effect = lambda obj: 'live' if obj == 10 else 'new serial'
        with contextlib.redirect_stdout(io.StringIO()):
            profile.restore()
        self.assertEqual(profile.restore_failures, 0)
        self.assertEqual(profile.retired_records, 1)
        self.assertNotIn(200, [call.args[0] for call in profile.game.write.call_args_list])


class ControllerPartialContexts(unittest.TestCase):
    def profile(self):
        profile, game = ControllerAxisBlocking().profile()
        array = profile.array
        def partial(*args):
            header, descriptor, rows = array(*args)
            return header, descriptor, [r for r in rows if r['action'] != 'IA_OpenUI_Emotes']
        profile.array = partial
        profile.swaps.clear(); profile.axis_blocks.clear(); profile.thresholds.clear()
        profile.collect = lambda:setattr(profile, 'active_thresholds', {
            profile.capture(50, 'EnhancedPlayerInput', 'EnhancedActionMappings')})
        return profile

    def test_incomplete_runtime_context_still_blocks_axis_without_guessing_button_pair(self):
        profile=self.profile()
        self.assertTrue(profile.update())
        self.assertEqual(profile.game.read(1000+0x28,8), bytes(8))
        self.assertFalse(profile.swaps)
        self.assertEqual(struct.unpack('<I', profile.game.read(1080+0x28,4))[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):profile.restore()
        self.assertEqual(profile.game.read(1000+0x28,8), profile.axis_key)
        self.assertEqual(profile.restore_failures,0)

    def test_unsupported_source_context_still_rejected(self):
        profile=self.profile()
        with self.assertRaisesRegex(RuntimeError,'Custom controller bindings'):
            profile.capture(200,'InputMappingContext','Mappings',source=True)
        self.assertEqual(profile.game.read(1000+0x28,8), profile.axis_key)


class ControllerAxisBlocking(unittest.TestCase):
    def profile(self, duplicate_r3=False):
        from test_keyboard_movement import Memory
        p = ControllerProfile.__new__(ControllerProfile)
        g = p.game = Memory()
        p.key_offset, p.trigger_offset = 0x28, 0
        p.swaps, p.thresholds, p.axis_blocks = {}, {}, {}
        p.axis_key = p.axis_cache = p.default_threshold = None
        p.r3_key = p.r3_cache = None
        p.last_update = 0
        p.camera = p.session = Mock()
        p.camera.identity.return_value = ('owner', 1)
        p.camera.is_a.return_value = True
        p.camera.field.return_value = {'address': 600, 'type': 'FloatProperty', 'size': 4}
        p.camera.reflection.header.return_value = {'class': 700}
        p.camera.identity.side_effect = lambda obj: ('owner', obj)
        count = 4 if duplicate_r3 else 3
        descriptor = struct.pack('<Qii', 1000, count, count)
        g.put(100, descriptor)
        g.put(500, struct.pack('<Q', 550))
        g.put(600, struct.pack('<f', .5))
        names = {1: 'Gamepad_Right2D', 2: 'Gamepad_LeftShoulder',
                 3: 'Gamepad_RightThumbstick', 0: 'None'}
        actions = ('IA_DodgeDirectional', 'IA_DodgeForward', 'IA_OpenUI_Emotes')
        if duplicate_r3:
            actions += ('IA_DodgeDirectional',)
        for i in range(count):
            raw = bytearray(0x50)
            if not i:
                struct.pack_into('<Qii', raw, 0, 500, 1, 1)
            struct.pack_into('<Q', raw, 0x20, 800+i)
            key_index = i+1 if i < 3 else 3
            cache_index = i if i < 3 else 2
            struct.pack_into('<IIQQ', raw, 0x28, key_index, 0, 10000+cache_index, 20000+cache_index)
            g.put(1000+i*0x50, raw)
        def array(*args):
            rows = []
            for i, action in enumerate(actions):
                addr = 1000+i*0x50
                raw = g.read(addr, 0x50)
                keys = raw[0x28:0x40]
                rows.append({'action': action, 'key': names[struct.unpack_from('<I', keys)[0]],
                             'keyBytes': keys, 'address': addr, 'tag': raw[:0x28], 'raw': raw})
            return 100, descriptor, rows
        p.array = array
        p.sources = [50]
        p.collect = lambda: setattr(p, 'active_thresholds', {p.capture(50, '', '', source=True)})
        p.collect()
        return p, g

    def test_axis_key_and_threshold_are_blocked_without_action_or_cache_changes(self):
        p, g = self.profile()
        before = g.read(1000, 0x50)
        p.update()
        self.assertEqual(g.read(1000+0x28, 8), bytes(8))
        self.assertEqual(g.read(1000, 0x28), before[:0x28])
        self.assertEqual(g.read(1000+0x30, 0x20), before[0x30:])
        self.assertEqual(g.read(600, 4), struct.pack('<f', 10))
        self.assertEqual(struct.unpack('<I', g.read(1080+0x28, 4))[0], 3)
        self.assertEqual(struct.unpack('<I', g.read(1160+0x28, 4))[0], 2)
        p.last_update = 0
        p.update()  # A disabled axis must still be discoverable on the next tick.
        with contextlib.redirect_stdout(io.StringIO()):
            p.restore()
        self.assertEqual(p.restore_failures, 0)
        self.assertEqual(g.read(1000, 0x50), before)
        self.assertEqual(g.read(600, 4), struct.pack('<f', .5))

    def test_runtime_clone_of_disabled_axis_is_restored_and_bookkeeping_preserved(self):
        p, g = self.profile()
        original_key = g.read(1000+0x28, 8)
        p.update()
        header, descriptor, rows = p.array()
        g.put(200, descriptor)
        from keyboard_movement import DisabledKey
        clone = DisabledKey(p, 60, 200, descriptor, 1000, original_key)
        self.assertTrue(clone.applied)
        g.put(1000+0x40, b'\x02')
        self.assertTrue(clone.restore())
        self.assertEqual(g.read(1000+0x28, 8), original_key)
        self.assertEqual(g.read(1000+0x40, 1), b'\x02')

    def test_stale_directional_r3_is_blocked_while_forward_r3_and_emotes_work(self):
        p, g = self.profile(duplicate_r3=True)
        action = g.read(1240+0x20, 8)
        p.update()
        self.assertEqual(g.read(1240+0x28, 8), bytes(8))
        self.assertEqual(g.read(1240+0x20, 8), action)
        self.assertEqual(struct.unpack('<I', g.read(1080+0x28, 4))[0], 3)
        self.assertEqual(struct.unpack('<I', g.read(1160+0x28, 4))[0], 2)
        p.last_update = 0
        p.update()
        with contextlib.redirect_stdout(io.StringIO()):
            p.restore()
        self.assertEqual(p.restore_failures, 0)
        self.assertEqual(struct.unpack('<I', g.read(1240+0x28, 4))[0], 3)

    def test_unfamiliar_runtime_trigger_keeps_native_trigger_and_blocks_only_the_axis_key(self):
        p, g = self.profile()
        p.camera.is_a.return_value = False
        p.thresholds.clear()
        p.collect = lambda: setattr(p, 'active_thresholds', set())
        self.assertIsNone(p.capture(50, '', '', source=False))
        p.update()
        self.assertEqual(g.read(600, 4), struct.pack('<f', .5))
        self.assertEqual(g.read(1000+0x28, 8), bytes(8))
        self.assertFalse(p.thresholds)

    def test_unfamiliar_source_trigger_is_still_rejected(self):
        p, g = self.profile()
        p.camera.is_a.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'Unexpected directional dodge trigger'):
            p.capture(50, '', '', source=True)

    def test_recycled_trigger_header_is_refreshed_before_classification(self):
        p, g = self.profile()
        p.camera.reflection.headers.pop.assert_called_with(550, None)

    def test_transient_rebuild_does_not_write_or_publish_incomplete_thresholds(self):
        from input_mapping_snapshot import MappingRebuilt
        p, g = self.profile()
        before = dict(g.storage) if hasattr(g, 'storage') else g.read(1000, 0xF0)
        p.collect = Mock(side_effect=MappingRebuilt('rebuilt'))
        active = p.active_thresholds.copy()
        self.assertFalse(p.update())
        self.assertEqual(p.active_thresholds, active)
        self.assertEqual(g.read(1000, 0xF0), before)
        self.assertEqual(g.read(600, 4), struct.pack('<f', .5))

    def test_inactive_surviving_trigger_is_not_suppressed_again(self):
        p, g = self.profile()
        p.collect = lambda: setattr(p, 'active_thresholds', set())
        p.update()
        self.assertEqual(g.read(600, 4), struct.pack('<f', .5))

    def test_retired_array_records_are_removed_after_a_complete_current_snapshot(self):
        p,g=self.profile()
        p.swaps['old']=Mock()
        p.swaps['old'].valid.return_value=False
        p.axis_blocks['old']=Mock()
        p.axis_blocks['old'].valid.return_value=False
        p.update()
        self.assertNotIn('old',p.swaps)
        self.assertNotIn('old',p.axis_blocks)
        self.assertEqual(len(p.swaps),1)
        self.assertEqual(len(p.axis_blocks),1)

    def test_retired_live_threshold_is_restored_before_its_record_is_removed(self):
        p,g=self.profile()
        p.update()
        p.collect=lambda:setattr(p,'active_thresholds',set())
        p.last_update=0
        p.update()
        self.assertEqual(g.read(600,4),struct.pack('<f',.5))
        self.assertFalse(p.thresholds)

    def test_menu_handoff_keeps_axis_blocked_and_restores_only_button_pair(self):
        p,g=self.profile()
        original = g.read(1000,0xF0)
        p.update()
        self.assertTrue(p.set_ui_input(True))
        self.assertEqual(g.read(1000+0x28,8),bytes(8))
        self.assertEqual(g.read(600,4),struct.pack('<f',10))
        self.assertEqual(struct.unpack('<I',g.read(1080+0x28,4))[0],2)
        self.assertEqual(struct.unpack('<I',g.read(1160+0x28,4))[0],3)
        self.assertTrue(p.set_ui_input(False))
        self.assertEqual(g.read(1000+0x28,8),bytes(8))
        self.assertEqual(struct.unpack('<I',g.read(1080+0x28,4))[0],3)
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertEqual(g.read(1000,0xF0),original)
        self.assertEqual(p.restore_failures,0)

    def test_native_axis_reset_is_blocked_on_next_frame_inside_scan_interval(self):
        p,g=self.profile()
        with patch('controller_profile.time.monotonic',return_value=100.):p.update()
        g.put(1000+0x28,p.axis_key)
        p.collect=Mock(side_effect=AssertionError('Full scan not needed'))
        with patch('controller_profile.time.monotonic',return_value=100.001):
            self.assertTrue(p.update())
        self.assertEqual(g.read(1000+0x28,8),bytes(8))

    def test_axis_write_failure_is_not_reported_as_ready(self):
        p,g=self.profile()
        original_write=g.write
        def dropped(address,data):
            if address != 1000+0x28:original_write(address,data)
        g.write=dropped
        with self.assertRaisesRegex(RuntimeError,'suppression did not persist'):p.update()

    def test_runtime_fkey_cache_change_gets_a_fresh_guarded_record(self):
        p,g=self.profile()
        p.update()
        g.put(1000+0x28,p.axis_key)
        g.put(1000+0x30,struct.pack('<QQ',98765,87654))
        p.collect=lambda:setattr(p,'active_thresholds',{p.capture(50,'','',source=False)})
        p.last_update=0
        self.assertTrue(p.update())
        self.assertEqual(g.read(1000+0x28,8),bytes(8))
        p.last_update=0
        self.assertTrue(p.update())
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertEqual(g.read(1000+0x28,8),p.axis_key)
        self.assertEqual(g.read(1000+0x30,16),struct.pack('<QQ',98765,87654))

    def test_disabled_owned_axis_with_new_cache_is_still_restored(self):
        p,g=self.profile()
        p.update()
        g.put(1000+0x30,struct.pack('<QQ',98765,87654))
        self.assertTrue(p.update())
        self.assertEqual(g.read(1000+0x28,8),bytes(8))
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertEqual(g.read(1000+0x28,8),p.axis_key)
        self.assertEqual(g.read(1000+0x30,16),struct.pack('<QQ',98765,87654))

    def test_rebuilt_runtime_with_duplicate_axes_blocks_both_and_restores_them(self):
        p,g=self.profile()
        original_axis=g.read(1000,0x50)
        p.update()
        descriptor=struct.pack('<Qii',1000,4,4)
        g.put(100,descriptor)
        g.put(1240,original_axis)
        array=p.array
        def duplicated(*args):
            header,_,rows=array(*args)
            raw=g.read(1240,0x50)
            rows.append(dict(address=1240,tag=raw[:0x28],raw=raw,
                action='IA_DodgeDirectional',key='Gamepad_Right2D' if raw[0x28:0x30]!=bytes(8) else 'None',
                keyBytes=raw[0x28:0x40]))
            return header,descriptor,rows
        p.array=duplicated
        p.collect=lambda:setattr(p,'active_thresholds',{p.capture(50,'','',source=False)})
        # Old record's descriptor is invalid, so even the fast frame recollects.
        self.assertTrue(p.update())
        for row in (1000,1240):self.assertEqual(g.read(row+0x28,8),bytes(8))
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        for row in (1000,1240):self.assertEqual(g.read(row+0x28,8),p.axis_key)
        self.assertEqual(p.restore_failures,0)

    def test_axis_retiring_during_enforcement_defers_input_and_forces_rescan(self):
        p,g=self.profile()
        p.update()
        block=next(iter(p.axis_blocks.values()))
        block.disable=Mock(return_value=False)
        self.assertFalse(p.update())
        self.assertEqual(p.last_update,0.)

    def test_empty_mapping_snapshot_replacement_also_forces_immediate_rescan(self):
        p,g=self.profile()
        header,descriptor,_=p.array()
        p.axis_blocks.clear();p.swaps.clear();p.thresholds.clear()
        p.array=lambda *args:(header,descriptor,[])
        p.camera.pointer.return_value=0
        p.collect=Mock(wraps=ControllerProfile.collect.__get__(p))
        with patch('controller_profile.time.monotonic',return_value=100.):
            self.assertTrue(p.update())
        self.assertEqual(len(p.mapping_arrays),1)
        p.collect.reset_mock()
        g.put(header,struct.pack('<Qii',4000,1,1))
        with patch('controller_profile.time.monotonic',return_value=100.001):
            self.assertFalse(p.update())
        p.collect.assert_called_once()
        self.assertEqual(p.last_update,0.)


if __name__ == '__main__':
    unittest.main()
