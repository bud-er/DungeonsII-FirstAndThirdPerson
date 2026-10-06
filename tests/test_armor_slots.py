from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from armor_slots import ArmorSlots, sparse_indices, MAP_OFFSET
from equipment_profile import EquipmentProfile
import test_equipment_profile


def sparse_header(pointer, count, *, bits=None, holes=0, heap=0):
    raw = bytearray(80)
    struct.pack_into('<Qii', raw, 0, pointer, count, max(24, count))
    if bits is None:bits = (1 << count)-1
    if not heap:raw[16:32] = bits.to_bytes(16, 'little')
    struct.pack_into('<Qii', raw, 32, heap, count, max(128, count))
    struct.pack_into('<ii', raw, 48, 1 if holes else -1, holes)
    return bytes(raw)


class ArmorDisplayMap(unittest.TestCase):
    def fixture(self):
        a = ArmorSlots.__new__(ArmorSlots)
        a.component, a.identity, a.pawn_identity = 0x110000, ('display', 1), ('pawn', 2)
        names = {1: 'SW.ItemSlot.Equipment.Armor.Helmet', 2: 'SW.ItemSlot.Equipment.Armor.Chest',
                 3: 'SW.ItemSlot.Equipment.Armor.Leggings', 4: 'SW.ItemSlot.Equipment.Armor.Boots',
                 5: 'SW.ItemSlot.Equipment.MeleeWeapon', 6: 'SW.ItemSlot.Equipment.Artifact.Slot1'}
        rows = bytearray()
        memory = {}
        for i, name in enumerate(names):
            pointer = 0x300000+i*0x100
            # Chest, pants and boots each own multiple components.
            count = 3 if name in (2, 3) else 2 if name == 4 else 1
            components = [0x400000+i*0x100+j*0x10 for j in range(count)]
            memory[pointer] = struct.pack('<'+'Q'*count, *components)
            rows += struct.pack('<IIQiiii', name, 0, pointer, count, count, -1, -1)
        memory[0x200000] = bytes(rows)
        memory[a.component+MAP_OFFSET] = sparse_header(0x200000, 6)
        def read(address, size):
            data = memory[address]
            self.assertEqual(len(data), size)
            return data
        game = SimpleNamespace(read=read, u64=lambda _: 0x120000)
        session = SimpleNamespace(pawn=0x120000, pc=0x130000,
            identity=lambda obj: a.identity if obj == a.component else a.pawn_identity,
            pointer=lambda *_: 0x120000,
            reflection=SimpleNamespace(name=lambda n, number=0: names[n]))
        a.game, a.session = game, session
        return a, memory

    def test_individual_slots_use_real_tags_and_include_sleeves_waist_and_both_boots(self):
        a, memory = self.fixture()
        for slot, count in (('helmet', 1), ('chest', 3), ('pants', 3), ('boots', 2)):
            selected = a.collect({slot})
            self.assertEqual(len(selected), count)
            self.assertEqual(set(selected.values()), {slot})
        selected = a.collect({'helmet', 'chest', 'pants', 'boots'})
        self.assertEqual(len(selected), 9)
        self.assertNotIn(0x400400, selected)  # Weapon.
        self.assertNotIn(0x400500, selected)  # Artifact.
        self.assertEqual(a.collect(set()), {})

    def test_slot_map_with_holes_does_not_read_retired_rows(self):
        a, memory = self.fixture()
        raw = bytearray(memory[0x200000]);raw[32:64] = bytes([255])*32
        memory[0x200000] = bytes(raw)
        memory[a.component+MAP_OFFSET] = sparse_header(0x200000, 6, bits=0b111101, holes=1)
        self.assertEqual(a.collect({'chest'}), {})
        self.assertEqual(len(a.collect({'helmet', 'boots'})), 3)

    def test_other_player_or_replaced_component_is_rejected_without_a_write_api(self):
        a, _ = self.fixture()
        a.game.u64 = lambda _: 0x999999
        with self.assertRaisesRegex(RuntimeError, 'owner changed'):
            a.collect({'helmet'})
        a, _ = self.fixture()
        a.session.identity = lambda _: ('replacement', 9)
        with self.assertRaises(RuntimeError):a.collect({'helmet'})

    def test_duplicate_components_between_slots_fail_closed(self):
        a, memory = self.fixture()
        boots = bytearray(memory[0x300300]);struct.pack_into('<Q', boots, 0, 0x400000)
        memory[0x300300] = bytes(boots)
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            a.collect({'helmet', 'boots'})

    def test_native_array_rebuild_is_not_used_as_a_visibility_target(self):
        a, memory = self.fixture()
        original = a.game.read
        calls = 0
        def changing(address, size):
            nonlocal calls
            if address == 0x300000:
                calls += 1
                if calls > 1:return struct.pack('<Q', 0x500000)
            return original(address, size)
        a.game.read = changing
        with self.assertRaisesRegex(RuntimeError, 'changed during inspection'):
            a.collect({'helmet'})

    def test_sparse_bounds_and_missing_allocation_flags_are_rejected(self):
        for offset, value in ((8, 999999), (12, -1), (40, 7), (52, 7)):
            raw = bytearray(sparse_header(0x200000, 6));struct.pack_into('<i', raw, offset, value)
            with self.subTest(offset=offset), self.assertRaises(RuntimeError):
                sparse_indices(bytes(raw), lambda *_: self.fail('invalid header read memory'))
        raw = sparse_header(0x200000, 129, bits=0)
        with self.assertRaisesRegex(RuntimeError, 'Missing'):
            sparse_indices(raw, lambda *_: self.fail('missing bitmap'))

    def test_heap_allocation_flags_are_supported_and_bounded(self):
        raw = sparse_header(0x200000, 129, heap=0x500000)
        flags = ((1 << 129)-1).to_bytes(20, 'little')
        read = Mock(return_value=flags)
        self.assertEqual(len(sparse_indices(raw, read)[2]), 129)
        read.assert_called_once_with(0x500000, 20)


class SharedVisibilityRestoration(unittest.TestCase):
    def profile(self, selected):
        record, memory = test_equipment_profile.EquipmentRestoration().make_record()
        profile = EquipmentProfile.__new__(EquipmentProfile)
        profile.hide_body, profile.next_scan = True, 0.
        profile.records, profile.desired = {100: record}, set()
        profile.restore_failures = set()
        profile.collect = lambda: setattr(profile, 'desired', {100} if profile.hide_body or selected else set())
        return profile, record, memory

    def test_selected_armor_stays_hidden_when_switching_to_shoulder_then_restores_on_exit(self):
        profile, record, memory = self.profile(True)
        profile.update()
        profile.update(hide_body=False)
        self.assertEqual(memory[800], b'\x38')
        profile.restore()
        self.assertEqual(memory[800], b'\x30')
        self.assertFalse(profile.restore_failures)

    def test_unselected_first_person_armor_returns_in_shoulder_view(self):
        profile, record, memory = self.profile(False)
        profile.update()
        profile.update(hide_body=False)
        self.assertEqual(memory[800], b'\x30')
        self.assertEqual(profile.records, {})

    def test_native_hidden_piece_is_never_forced_visible_when_helper_stops(self):
        profile, record, memory = self.profile(True)
        record.original = 8;memory[800] = b'\x38'
        profile.update();profile.restore()
        self.assertEqual(memory[800], b'\x38')


if __name__ == '__main__':
    unittest.main()
