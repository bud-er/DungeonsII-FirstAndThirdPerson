from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from equipment_profile import EquipmentVisibility


class EquipmentRestoration(unittest.TestCase):
    def make_record(self):
        record = EquipmentVisibility.__new__(EquipmentVisibility)
        record.obj, record.identity, record.parent, record.socket = 100, ('mesh', 1), 200, (30, 0)
        record.relative, record.world = 300, 400
        record.hidden, record.hidden_mask, record.original = 800, 8, 0
        record.dirty, record.registered = 900, 901
        record.replicated = {'address': 500, 'bool': {'byteMask': 128}}
        record.collision, record.simulation, record.simulation_mask = 600, 700, 1
        record.applied = False
        storage = {300: struct.pack('<ddd', -1, 1, 1), 400: struct.pack('<ddd', -1, 1, 1),
                   500: b'\x03', 600: b'\0', 700: b'\x60', 800: b'\x30', 900: b'\x60', 901: b'\x03'}
        game, session = Mock(), Mock()
        game.read.side_effect = lambda address, size: storage[address]
        game.write.side_effect = lambda address, value: storage.__setitem__(address, value)
        session.identity.return_value = record.identity
        session.pointer.return_value = record.parent
        session.value.return_value = record.socket
        record.profile = SimpleNamespace(game=game, session=session)
        return record, storage

    def test_native_visibility_restores_flags_and_never_changes_mirrored_scale(self):
        r, data = self.make_record()
        self.assertTrue(r.update())
        self.assertEqual(data[800], b'\x38')
        self.assertEqual(data[900], b'\x61')
        data[900] = b'\x60'  # Engine consumed the rendering refresh.
        self.assertTrue(r.restore())
        self.assertEqual(data[300], struct.pack('<ddd', -1, 1, 1))
        self.assertEqual(data[400], data[300])
        self.assertEqual(data[800], b'\x30')
        self.assertEqual(data[900], b'\x61')
        self.assertFalse({300,400} & {call.args[0] for call in r.profile.game.write.call_args_list})

    def test_no_writes_to_replicated_or_colliding_equipment(self):
        for address, value in ((500, b'\x80'), (600, b'\x01'), (700, b'\x01')):
            r, data = self.make_record()
            data[address] = value
            self.assertFalse(r.update())
            r.profile.game.write.assert_not_called()

    def test_removed_or_reparented_equipment_is_not_written(self):
        for changed in ('identity', 'parent', 'socket'):
            r, data = self.make_record()
            r.applied = True
            if changed == 'identity': r.profile.session.identity.return_value = ('new', 2)
            if changed == 'parent': r.profile.session.pointer.return_value = 900
            if changed == 'socket': r.profile.session.value.return_value = (99, 0)
            self.assertFalse(r.update())
            self.assertTrue(r.restore())
            r.profile.game.write.assert_not_called()

    def test_partial_apply_is_still_restored(self):
        r, data = self.make_record()
        original_write = r.profile.game.write.side_effect
        def fail_world(address, value):
            if address == 900: raise OSError('render flag write failed')
            original_write(address, value)
        r.profile.game.write.side_effect = fail_world
        with self.assertRaises(OSError): r.update()
        self.assertTrue(r.applied)
        r.profile.game.write.side_effect = original_write
        self.assertTrue(r.restore())
        self.assertEqual(data[800], b'\x30')

    def test_failed_readback_remains_retryable(self):
        r, data = self.make_record()
        r.update()
        r.profile.game.write.side_effect = None
        self.assertFalse(r.restore())
        self.assertTrue(r.applied)


if __name__ == '__main__':
    unittest.main()
