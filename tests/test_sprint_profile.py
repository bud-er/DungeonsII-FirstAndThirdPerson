from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from sprint_profile import SprintProfile, NATIVE_GUARDS, CONTENTION_REASON
from native_combat_activity import ActivityUnavailable


class Memory:
    def __init__(self):
        self.base, self.data, self.writes = 0x10000000, {}, []

    def read(self, address, size):
        return bytes(self.data.get(address+i, 0) for i in range(size))

    def put(self, address, data):
        self.data.update({address+i: byte for i, byte in enumerate(data)})

    def write(self, address, data):
        self.writes.append((address, data))
        self.put(address, data)

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]


class SprintTests(unittest.TestCase):
    def session(self):
        g = Memory()
        move = 0x4000
        s = SimpleNamespace(game=g, pawn=0x2000, pc=0x1000, identities={move: 'original'},
            ensure_layout=Mock(), remember=Mock(), pointer=Mock(return_value=move),
            identity=Mock(return_value='original'), object_retired=Mock(return_value=False),
            reflection=Mock(header=Mock(return_value={'outer': 0x2000})), local_scope=Mock())
        def field(obj, cls, name):
            offset, kind, size = {'MaxWalkSpeed': (0x278, 'FloatProperty', 4),
                                 'MovementMode': (0x231, 'ByteProperty', 1)}[name]
            return dict(offset=offset, type=kind, size=size, dimensions=1, address=obj+offset)
        s.field = field
        for rva, data in NATIVE_GUARDS.items():
            g.put(g.base+rva, data)
        g.put(move, struct.pack('<Q', 0x8000))
        g.put(0x8000+157*8, struct.pack('<Q', g.base+0x63B1F30))
        g.put(0x8000+390*8, struct.pack('<Q', g.base+0x63BC390))
        g.put(move+0x278, struct.pack('<f', 600.))
        g.put(move+0x124C, struct.pack('<f', 1.))
        g.put(move+0x1260, struct.pack('<f', 1.))
        g.put(move+0x231, b'\x01')
        return s

    def profile(self):
        with patch('sprint_profile.NativeCombatActivity') as activity:
            activity.return_value.snapshot.return_value = (False, False)
            p = SprintProfile(self.session())
        return p

    def speed(self, p):
        return struct.unpack('<f', p.game.read(p.speed, 4))[0]

    def parameter(self, p):
        return struct.unpack('<f', p.game.read(p.parameter, 4))[0]

    def start(self, p):
        p.set_input(True)
        p.update()

    def test_constructor_is_read_only_and_validates_native_getter(self):
        p = self.profile()
        self.assertFalse(p.game.writes)
        s = self.session()
        s.game.put(0x8000+157*8, bytes(8))
        with self.assertRaisesRegex(RuntimeError, 'getter'):
            SprintProfile(s)
        self.assertFalse(s.game.writes)

    def test_code_or_property_mismatch_fails_before_writes(self):
        for change in ('code', 'property'):
            s = self.session()
            if change == 'code':
                s.game.put(s.game.base+0x63B1F30, b'\0')
            else:
                field = s.field
                s.field = lambda *args: dict(field(*args), offset=999)
            with self.assertRaises(RuntimeError):
                SprintProfile(s)
            self.assertFalse(s.game.writes)

    def test_sprint_scales_once_and_restores_exact_original(self):
        p = self.profile()
        self.start(p)
        self.assertAlmostEqual(self.parameter(p), 1.2)
        self.assertEqual(self.speed(p), 600.)  # Native output is not written.
        for _ in range(10):
            p.update()
        self.assertEqual(len(p.game.writes), 1)
        p.set_input(False)
        p.update()
        self.assertEqual(self.speed(p), 600.)
        self.assertEqual(self.parameter(p), 1.)
        self.assertFalse(p.applied)
        self.assertEqual({a for a, _ in p.game.writes}, {p.parameter})

    def test_native_writer_computes_speed_and_buffs_without_external_output_writes(self):
        p = self.profile(); p.multiplier = 1.3
        p.game.put(p.speed, struct.pack('<f', 700.)); self.start(p)
        self.assertEqual(self.speed(p), 700.)
        def native_tick(base):
            p.game.put(p.speed, struct.pack('<f', base*self.parameter(p)))
        for _ in range(100):
            native_tick(700.); p.update()
        self.assertAlmostEqual(self.speed(p), 910., places=3)
        self.assertEqual(p.native_refreshes, 0)
        self.assertEqual(len(p.game.writes), 1)
        native_tick(805.); p.update()
        self.assertAlmostEqual(self.speed(p), 1046.5, places=3)
        self.assertTrue(p.applied)
        p.restore()
        self.assertEqual(self.parameter(p), 1.)
        self.assertAlmostEqual(self.speed(p), 1046.5, places=3)
        native_tick(805.)
        self.assertEqual(self.speed(p), 805.)
        self.assertEqual({address for address, _ in p.game.writes}, {p.parameter})

    def test_output_baseline_refreshes_do_not_retry_parameter_or_pause(self):
        p = self.profile(); self.start(p)
        for _ in range(100):
            p.game.put(p.speed, struct.pack('<f', 600.)); p.update()
        self.assertEqual(len(p.game.writes), 1)
        self.assertEqual(p.native_refreshes, 0)
        self.assertIsNone(p.paused_reason)
        self.assertAlmostEqual(self.parameter(p), 1.2)

    def test_active_default_change_cancels_and_preserves_native_default_and_speed(self):
        p = self.profile(); self.start(p)
        p.game.put(p.default_parameter, struct.pack('<f', .5))
        p.game.put(p.speed, struct.pack('<f', 350.)); p.update()
        self.assertEqual(self.parameter(p), 1.)
        self.assertEqual(p.game.read(p.default_parameter, 4), struct.pack('<f', .5))
        self.assertEqual(self.speed(p), 350.)
        self.assertTrue(p.interrupted)
        self.assertFalse(p.applied)
        self.assertEqual({address for address, _ in p.game.writes}, {p.parameter})

    def test_invalid_native_output_cancels_only_owned_parameter(self):
        for value in (float('nan'), float('inf'), 0., -1., 4051.):
            p = self.profile(); self.start(p)
            raw = struct.pack('<f', value); p.game.put(p.speed, raw); p.update()
            self.assertEqual(self.parameter(p), 1.)
            self.assertEqual(p.game.read(p.speed, 4), raw)
            self.assertTrue(p.interrupted)
            self.assertEqual({address for address, _ in p.game.writes}, {p.parameter})

    def test_unvalidated_parameter_baselines_refuse_activation(self):
        for value in (float('nan'), float('inf'), 0., -1., .5, 1.15, 2.):
            p = self.profile(); p.game.put(p.parameter, struct.pack('<f', value))
            self.start(p)
            self.assertFalse(p.game.writes)
            self.assertTrue(p.interrupted)

    def test_every_native_guard_and_writer_vtable_are_required(self):
        for rva in NATIVE_GUARDS:
            s = self.session(); s.game.put(s.game.base+rva, b'\xCC')
            with self.assertRaises(RuntimeError): SprintProfile(s)
            self.assertFalse(s.game.writes)
        s = self.session(); s.game.put(0x8000+390*8, bytes(8))
        with self.assertRaisesRegex(RuntimeError, 'writer'): SprintProfile(s)
        self.assertFalse(s.game.writes)

    def test_focus_or_menu_restores_and_clears_request(self):
        p = self.profile(); self.start(p)
        p.set_input(True, eligible=False); p.update()
        self.assertEqual(self.parameter(p), 1.)
        self.assertFalse(p.requested)
        self.start(p); p.restore()
        self.assertEqual(self.parameter(p), 1.)
        self.assertFalse(p.restore_failures)

    def test_attack_roll_airborne_and_movement_lock_cancel_sprint(self):
        for condition in ('attack', 'roll', 'air', 'lock', 'unknown'):
            with self.subTest(condition=condition):
                p = self.profile(); self.start(p)
                if condition in ('attack', 'roll'):
                    p.activity.snapshot.return_value = (condition == 'attack', condition == 'roll')
                elif condition == 'air': p.game.put(p.mode, b'\x03')
                elif condition == 'lock': p.game.put(p.session.pc+0x339, b'\x01')
                else: p.activity.snapshot.side_effect = ActivityUnavailable('handoff')
                p.update()
                self.assertEqual(self.parameter(p), 1.)
                self.assertTrue(p.interrupted)
                self.assertFalse(p.requested)

    def test_native_parameter_change_wins_and_is_never_adopted_or_compounded(self):
        p = self.profile(); self.start(p)
        p.game.put(p.parameter, struct.pack('<f', 1.15))
        p.update(); p.restore()
        self.assertAlmostEqual(self.parameter(p), 1.15)
        self.assertEqual(len(p.game.writes), 1)
        self.assertTrue(p.interrupted)
        self.start(p)
        self.assertAlmostEqual(self.parameter(p), 1.15)
        self.assertEqual(len(p.game.writes), 1)
        p.restore()
        self.assertAlmostEqual(self.parameter(p), 1.15)

    def test_spaced_baseline_refresh_retains_one_fixed_boost(self):
        p = self.profile(); self.start(p)
        for index in range(20):
            p.game.put(p.parameter, struct.pack('<f', 1.))
            with patch('sprint_profile.time.monotonic', return_value=index*.3):
                p.update()
            self.assertAlmostEqual(self.parameter(p), 1.2)
            self.assertTrue(p.requested)
            self.assertFalse(p.interrupted)
        self.assertEqual(p.native_refreshes, 20)
        self.assertEqual(p.activations, 1)
        self.assertEqual({data for _,data in p.game.writes}, {struct.pack('<f', 1.2)})
        p.restore()
        self.assertEqual(self.parameter(p), 1.)

    def test_rapid_baseline_refresh_pauses_before_fourth_rewrite_and_preserves_preferences(self):
        p = self.profile(); p.multiplier = 1.3; self.start(p)
        for index in range(4):
            p.game.put(p.parameter, struct.pack('<f', 1.))
            with patch('sprint_profile.time.monotonic', return_value=10.+index*.02):
                p.update()
        self.assertEqual(len(p.game.writes), 4)  # Initial write and three retries.
        self.assertEqual(self.parameter(p), 1.)
        self.assertTrue(p.interrupted)
        self.assertFalse(p.applied)
        self.assertEqual(p.multiplier, 1.3)
        self.assertEqual(p.diagnostics()['paused_reason'], CONTENTION_REASON)
        self.assertEqual(p.diagnostics()['native_refreshes'], 4)
        for _ in range(5):
            p.set_input(True); p.update()
        self.assertEqual(len(p.game.writes), 4)
        self.assertEqual(p.diagnostics()['last_stop_reason'], CONTENTION_REASON)

    def test_contention_pause_survives_camera_recreation_but_not_a_new_game_connection(self):
        p = self.profile(); p.game._sprint_contention_reason = CONTENTION_REASON
        with patch('sprint_profile.NativeCombatActivity'):
            recreated = SprintProfile(p.session, multiplier=1.3)
        self.start(recreated)
        self.assertEqual(recreated.paused_reason, CONTENTION_REASON)
        self.assertFalse(recreated.game.writes)
        self.assertTrue(recreated.interrupted)
        fresh = self.profile(); self.start(fresh)
        self.assertIsNone(fresh.paused_reason)
        self.assertAlmostEqual(self.parameter(fresh), 1.2)

    def test_contention_detected_across_brief_stops_without_accumulating_old_refreshes(self):
        p = self.profile()
        for index in range(4):
            self.start(p)
            p.game.put(p.parameter, struct.pack('<f', 1.))
            with patch('sprint_profile.time.monotonic', return_value=10.+index*.02):
                p.update()
            p.restore()
        self.assertEqual(p.paused_reason, CONTENTION_REASON)
        self.assertEqual(self.parameter(p), 1.)

    def test_no_role_based_sprint_block_or_pause_without_observed_refreshes(self):
        for role in (2, 3):
            p = self.profile(); p.session.local_scope.validate.return_value = role
            self.start(p)
            for _ in range(20): p.update()
            self.assertAlmostEqual(self.parameter(p), 1.2)
            self.assertIsNone(p.paused_reason)

    def test_native_baseline_refresh_during_write_does_not_cancel_intent(self):
        p = self.profile()
        write = p.game.write
        def refresh(address, data):
            write(address, data)
            p.game.put(address, struct.pack('<f', 1.))
        p.game.write = refresh
        self.start(p)
        p.update()
        self.assertTrue(p.requested)
        self.assertFalse(p.interrupted)
        self.assertEqual(p.diagnostics()['observed_speed'], 600.)
        self.assertEqual(p.diagnostics()['observed_parameter'], 1.)
        self.assertAlmostEqual(p.diagnostics()['target_parameter'], 1.2)
        self.assertEqual(p.native_refreshes, 1)
        self.assertEqual(len(p.game.writes), 2)  # One bounded write per tick.
        p.restore()
        self.assertEqual(len(p.game.writes), 2)  # Native baseline already restored.

    def test_cancellation_reason_survives_idle_ticks_for_later_snapshot(self):
        p = self.profile(); self.start(p)
        p.game.put(p.mode, b'\x03')
        p.update()
        p.set_input(False); p.update()
        self.assertEqual(p.diagnostics()['last_stop_reason'], 'not walking')
        self.assertEqual(p.diagnostics()['reason'], 'idle')
        self.assertEqual(p.diagnostics()['observed_speed'], 600.)

    def test_native_change_at_final_check_is_preserved_without_a_write(self):
        p = self.profile()
        read = p.game.read
        reads = [0]
        def change(address, size):
            if address == p.parameter:
                reads[0] += 1
                if reads[0] == 2:
                    p.game.put(address, struct.pack('<f', 1.15))
            return read(address, size)
        p.game.read = change
        self.start(p)
        self.assertAlmostEqual(self.parameter(p), 1.15)
        self.assertFalse(p.game.writes)
        self.assertTrue(p.interrupted)

    def test_native_change_immediately_after_write_is_never_restored_over(self):
        p = self.profile()
        write = p.game.write
        def change(address, data):
            write(address, data)
            p.game.put(address, struct.pack('<f', 1.15))
        p.game.write = change
        self.start(p)
        self.assertAlmostEqual(self.parameter(p), 1.15)
        self.assertEqual(len(p.game.writes), 1)
        self.assertEqual(p.diagnostics()['last_stop_reason'], 'native walking parameter changed')
        self.assertFalse(p.applied)

    def test_no_speed_write_for_invalid_baseline(self):
        for value in (float('nan'), float('inf'), 0., -1., 3001.):
            p = self.profile()
            p.game.put(p.speed, struct.pack('<f', value))
            self.start(p)
            self.assertFalse(p.game.writes)
            self.assertTrue(p.interrupted)

    def test_stale_or_remote_possession_prevents_sprint(self):
        p = self.profile()
        p.session.local_scope.validate.side_effect = RuntimeError('remote pawn')
        with self.assertRaises(RuntimeError): self.start(p)
        self.assertFalse(p.game.writes)

    def test_retired_component_is_never_dereferenced_or_written_on_restore(self):
        p = self.profile(); self.start(p)
        p.session.object_retired.return_value = True
        p.game.read = Mock(side_effect=AssertionError('retired read'))
        p.game.writes.clear(); p.restore()
        self.assertFalse(p.game.writes)
        self.assertFalse(p.applied)

    def test_unreadable_component_keeps_restoration_record_for_retry(self):
        p = self.profile(); self.start(p)
        p.session.object_retired.side_effect = OSError('temporary read failure')
        p.restore()
        self.assertTrue(p.restore_failures)
        self.assertTrue(p.applied)
        p.session.object_retired.side_effect = None
        p.restore()
        self.assertEqual(self.parameter(p), 1.)
        self.assertFalse(p.restore_failures)

    def test_restore_old_local_component_after_possession_changed(self):
        p = self.profile(); self.start(p)
        p.session.local_scope.validate.side_effect = RuntimeError('new pawn')
        p.restore()
        self.assertEqual(self.parameter(p), 1.)

    def test_partial_write_failure_still_restores(self):
        p = self.profile()
        write = p.game.write
        def fail(address, data):
            write(address, data)
            raise OSError('write completed but status failed')
        p.game.write = fail
        with self.assertRaises(OSError): self.start(p)
        p.game.write = write
        p.restore()
        self.assertEqual(self.parameter(p), 1.)

    def test_multiplier_limits(self):
        for value in (True, .5, 1., 1.36, float('nan'), float('inf')):
            with self.assertRaises(ValueError): SprintProfile(self.session(), multiplier=value)


if __name__ == '__main__': unittest.main()
