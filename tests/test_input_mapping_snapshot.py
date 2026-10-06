import struct
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from input_mapping_snapshot import mapping_snapshot, checked_row, MappingRebuilt


class InputSnapshots(unittest.TestCase):
    def test_changed_action_cannot_be_accepted_under_a_stable_array_descriptor(self):
        descriptor = struct.pack('<Qii',100,1,1)
        expected = bytes(0x50)
        changed = bytearray(expected)
        struct.pack_into('<Q',changed,0x20,999)
        game = Mock()
        game.read.side_effect=[descriptor,bytes(changed)]
        with self.assertRaises(MappingRebuilt):
            checked_row(game,10,descriptor,100,expected)
        game.write.assert_not_called()

    def test_rebuilt_buffer_is_retried_using_only_the_new_snapshot(self):
        old, new = (struct.pack('<Qii', p, 1, 1) for p in (100, 200))
        game = Mock()
        game.read.side_effect = [old, b'old', new, new, b'new', new]
        self.assertEqual(mapping_snapshot(game, 10, 3), (new, b'new'))
        game.write.assert_not_called()

    def test_freed_buffer_is_retried_only_after_a_changed_descriptor(self):
        old, new = (struct.pack('<Qii', p, 1, 1) for p in (100, 200))
        game = Mock()
        game.read.side_effect = [old, OSError('freed'), new, new, b'new', new]
        self.assertEqual(mapping_snapshot(game, 10, 3), (new, b'new'))

    def test_stable_unreadable_buffer_fails_closed(self):
        descriptor = struct.pack('<Qii', 100, 1, 1)
        game = Mock()
        game.read.side_effect = [descriptor, OSError('unavailable'), descriptor]
        with self.assertRaises(OSError):
            mapping_snapshot(game, 10, 3)
        game.write.assert_not_called()

    def test_continuous_rebuild_has_a_bounded_retry_without_writes(self):
        old, new = (struct.pack('<Qii', p, 1, 1) for p in (100, 200))
        game = Mock()
        game.read.side_effect = [old, b'old', new] * 3
        with self.assertRaises(MappingRebuilt):
            mapping_snapshot(game, 10, 3)
        self.assertEqual(game.read.call_count, 9)
        game.write.assert_not_called()

    def test_invalid_array_is_not_retried_or_read(self):
        game = Mock()
        game.read.return_value = struct.pack('<Qii', 0, 1, 1)
        with self.assertRaises(RuntimeError):
            mapping_snapshot(game, 10)
        self.assertEqual(game.read.call_count, 1)

