from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from avatar_profile import AvatarProfile, HIDDEN, hide_body, yaw_quaternion


class Memory:
    def __init__(self):
        self.cells, self.writes = {}, []

    def read(self, address, size):
        result = self.cells[address]
        if len(result) != size:
            raise AssertionError((address, size, len(result)))
        return result

    def write(self, address, value):
        self.writes.append((address, value))
        self.cells[address] = value


class AvatarRestoration(unittest.TestCase):
    def make_profile(self):
        a = AvatarProfile.__new__(AvatarProfile)
        a.mesh, a.asset = 1000, 2000
        a.session = SimpleNamespace(root=3000, identities={1000: 1, 2000: 2, 3000: 3},
                                    identity=Mock(side_effect=lambda p: {1000: 1, 2000: 2, 3000: 3}[p]))
        a.game = Memory()
        a.buffers = [(4000, struct.pack('<Qii', 5000, 32, 32), 5000, bytes([1]*32), hide_body(bytes([1]*32))),
                     (6000, struct.pack('<Qii', 7000, 32, 32), 7000, bytes([1]*32), hide_body(bytes([1]*32)))]
        for address, header, pointer, original, hidden in a.buffers:
            a.game.cells[address] = header
            a.game.cells[pointer] = hidden
        a.rotation = 8000
        a.saved_rotation = struct.pack('<ddd', 0, -90, 0)
        a.absolute_address, a.absolute_mask, a.saved_absolute = 9000, 4, 0
        a.game.cells[9000] = b'\xC4'  # Unrelated game flags must survive.
        a.dirty = {'address': 10000, 'bool': {'byteMask': 32}}
        a.game.cells[10000] = b'\xA1'
        a.game.cells[3000+0x1E0] = struct.pack('<dddd', *yaw_quaternion(45))
        a.restore_failures, a.applied, a.yaw_offset = set(), True, -90
        return a

    def test_hiding_torso_preserves_arms_weapons_and_root(self):
        baseline = bytes([1]*32)
        hidden = hide_body(baseline)
        self.assertTrue(all(hidden[i] == 2 for i in HIDDEN))
        self.assertTrue(all(hidden[i] == 1 for i in range(32) if i not in HIDDEN))
        self.assertEqual(hidden[8:21], baseline[8:21])

    def test_existing_hidden_weapon_state_is_preserved(self):
        baseline = bytearray([1]*32)
        baseline[11] = 2
        self.assertEqual(hide_body(baseline)[11], 2)

    def test_native_facing_comparison_only_hides_and_restores_visibility(self):
        a=self.make_profile();a.follow_view=False;a.validate=Mock();a.equipment=Mock(restore_failures=set())
        a.applied=False;a.update()
        self.assertTrue(all(address in (5000,7000,10000) for address,_ in a.game.writes))
        a.game.writes.clear();a.restore()
        self.assertEqual(a.game.cells[5000],bytes([1]*32))
        self.assertFalse(a.restore_failures)
        self.assertTrue(all(address in (5000,7000,10000) for address,_ in a.game.writes))

    def test_unknown_visibility_data_is_rejected(self):
        for invalid in (bytes(31), bytes([3]*32)):
            with self.assertRaises(RuntimeError):
                hide_body(invalid)

    def test_restore_uses_current_parent_yaw_and_preserves_other_flags(self):
        a = self.make_profile()
        a.restore()
        self.assertFalse(a.restore_failures)
        self.assertFalse(a.applied)
        self.assertEqual(a.game.cells[9000], b'\xC0')
        restored = struct.unpack('<dddd', a.game.cells[a.mesh+0x1E0])
        for actual, expected in zip(restored, yaw_quaternion(-45)):
            self.assertAlmostEqual(actual, expected)

    def test_changed_allocation_is_skipped_but_other_buffer_restores(self):
        a = self.make_profile()
        a.game.cells[4000] = struct.pack('<Qii', 15000, 32, 32)
        a.restore()
        self.assertEqual(a.restore_failures, {4000})
        self.assertNotIn(5000, [address for address, _ in a.game.writes])
        self.assertEqual(a.game.cells[7000], bytes([1]*32))
        self.assertTrue(a.applied)

    def test_recycled_mesh_is_never_restored(self):
        a = self.make_profile()
        a.session.identity.side_effect = lambda p: 999
        a.restore()
        self.assertFalse(a.game.writes)
        self.assertEqual(a.restore_failures, {'mesh identity'})

    def test_partial_apply_is_restorable(self):
        a = self.make_profile()
        a.validate = Mock()
        a.applied = False
        a.session.yaw = 80
        real_write = a.game.write
        calls = 0
        def failing_write(address, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('Second buffer write failed')
            real_write(address, data)
        a.game.write = failing_write
        with self.assertRaises(OSError):
            a.update()
        self.assertTrue(a.applied)
        a.game.write = real_write
        a.restore()
        self.assertFalse(a.restore_failures)
        self.assertEqual(a.game.cells[5000], bytes([1]*32))

    def test_roll_force_steering_leaves_avatar_facing_the_view(self):
        a=self.make_profile();a.validate=Mock();a.equipment=Mock()
        a.session.yaw=90.
        a.session.combat_profile=SimpleNamespace(animation_yaw=-90.)
        a.update()
        self.assertEqual(struct.unpack('<ddd',a.game.cells[a.rotation]),(0.,0.,0.))
        self.assertEqual(a.session.yaw,90.)
        for value,expected in zip(struct.unpack('<dddd',a.game.cells[a.mesh+0x1E0]),yaw_quaternion(0.)):
            self.assertAlmostEqual(value,expected)
        a.session.combat_profile.animation_yaw=90.
        a.update()
        self.assertEqual(struct.unpack('<ddd',a.game.cells[a.rotation]),(0.,0.,0.))


if __name__ == '__main__':
    unittest.main()
