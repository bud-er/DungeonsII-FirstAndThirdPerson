from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from local_menu_scope import (GUARDS, LocalMenuScope, MenuPresentationChanging,
                              OWNER_GETTER_RVA, weak_token)
from test_keyboard_movement import Memory


class MenuScopeTests(unittest.TestCase):
    def fixture(self):
        g = Memory()
        g.base = 0x140000000
        ids, objects = {}, {}
        offsets = {'Player':0x100, 'OwningWorld':0x100, 'Visibility':0xDC,
                   'Slot':0x30, 'Parent':0x28, 'Content':0x30,
                   'WidgetTree':0x2D8, 'RootWidget':0x30, 'DisplayedWidget':0x1A0}
        ancestors = {'Menu':('UserWidget','Widget'), 'Root':('UserWidget','Widget'),
                     'Panel':('PanelWidget','Widget'),
                     'Stack':('CommonActivatableWidgetContainerBase','Widget')}
        s = SimpleNamespace(game=g, pc=0x1000, ensure_layout=Mock())
        s.identity = lambda a: ids[a]
        s.object_retired = lambda a, identity: ids.get(a) != identity
        s.is_a = lambda cls, target: cls == target or target in ancestors.get(cls, ())
        s.reflection = SimpleNamespace(headers={}, header=lambda a: objects[a])
        s.field = lambda a, cls, name: dict(address=a+offsets[name],
            type='EnumProperty' if name=='Visibility' else 'ObjectProperty',
            size=1 if name=='Visibility' else 8)
        s.pointer = lambda a, cls, name: g.u64(a+offsets[name])

        def add(a, cls, outer=0):
            ids[a] = (struct.pack('<I', len(ids)+1)+bytes(16), struct.pack('<I', len(ids)+10))
            objects[a] = dict(address=a, **{'class':cls}, outer=outer, flags=0)
            g.put(a+0xDC, b'\x04')
            g.put(a, struct.pack('<Q', 0xB000))
            return a

        def link(a, name, target):
            g.put(a+offsets[name], struct.pack('<Q', target))

        add(s.pc, 'PlayerController', 0x2000)
        add(0x2000, 'Level');add(0x3000, 'World');add(0x4000, 'LocalPlayer')
        link(s.pc, 'Player', 0x4000);link(0x2000, 'OwningWorld', 0x3000)
        root = add(0x5000, 'Root')
        tree = add(0x6000, 'WidgetTree', root)
        panel = add(0x7000, 'Panel', tree)
        slot = add(0x8000, 'PanelSlot', panel)
        stack = add(0x9000, 'Stack', tree)
        menu = add(0xA000, 'Menu', tree)
        viewport = add(0xC000, 'GameViewportSubsystem')
        for a in (root, menu):
            g.put(a+0x328, weak_token(ids[0x4000])+bytes(8))
        g.put(0xB000+0x2E0, struct.pack('<Q', g.base+OWNER_GETTER_RVA))
        link(root, 'WidgetTree', tree);link(tree, 'RootWidget', panel)
        link(slot, 'Parent', panel);link(slot, 'Content', stack)
        link(stack, 'Slot', slot);link(stack, 'DisplayedWidget', menu)
        g.put(root+0xD9, b'\x20')
        raw = bytearray(0x50)
        struct.pack_into('<Qii', raw, 0, 0xD000, 1, 1)
        raw[0x10] = 1
        struct.pack_into('<iiii', raw, 0x28, 1, 128, -1, 0)
        g.put(viewport+0x60, raw)
        node = bytearray(0x78);node[:8] = weak_token(ids[root])
        struct.pack_into('<QQ', node, 0x58, 0xE000, 0xF000)
        g.put(0xD000, node);g.put(0xF008, struct.pack('<i', 3))
        for rva, expected in GUARDS.items():
            g.put(g.base+rva, expected)
        scope = LocalMenuScope(s)
        for obj in objects.values():
            scope.consider(obj)
        scope.begin()
        return scope, g, ids, objects, link

    def test_local_displayed_menu_reaches_live_viewport_without_native_capture(self):
        scope, g, ids, _, _ = self.fixture()
        self.assertTrue(scope.presented(0xA000, ids[0xA000]))
        self.assertEqual(g.writes, [])

    def test_stale_active_map_off_stack_is_not_presented(self):
        scope, _, ids, _, link = self.fixture()
        link(0x9000, 'DisplayedWidget', 0)
        scope.begin()
        self.assertFalse(scope.presented(0xA000, ids[0xA000]))

    def test_hidden_parent_blocks_visible_menu(self):
        for hidden in (1, 2):
            scope, g, ids, _, _ = self.fixture()
            g.put(0x7000+0xDC, bytes([hidden]))
            self.assertFalse(scope.presented(0xA000, ids[0xA000]))

    def test_foreign_player_or_world_cannot_capture_local_input(self):
        for offset in (0x328, 0x330):
            scope, g, ids, _, _ = self.fixture()
            g.put(0xA000+offset, struct.pack('<II', 900, 901))
            self.assertFalse(scope.presented(0xA000, ids[0xA000]))

    def test_reused_widget_serial_does_not_match_saved_menu(self):
        scope, _, ids, _, _ = self.fixture()
        original = ids[0xA000]
        ids[0xA000] = (original[0], struct.pack('<I', 999))
        self.assertFalse(scope.presented(0xA000, original))

    def test_viewport_membership_requires_matching_serial_allocated_slot_and_live_slate(self):
        mutations = ((0xD004, struct.pack('<I', 999)),
                     (0xD058, bytes(8)), (0xD060, bytes(8)),
                     (0xF008, struct.pack('<i', 0)))
        for address, value in mutations:
            scope, g, ids, _, _ = self.fixture()
            g.put(address, value)
            self.assertFalse(scope.presented(0xA000, ids[0xA000]))
        scope, g, ids, _, _ = self.fixture()
        g.put(0xC070, b'\0');g.put(0xC094, struct.pack('<i', 1))
        self.assertFalse(scope.presented(0xA000, ids[0xA000]))

    def test_positive_slate_reference_count_can_change_during_read(self):
        scope, g, ids, _, _ = self.fixture()
        original = g.read
        counts = iter((3, 4))
        g.read = lambda a, n: struct.pack('<i', next(counts)) if a==0xF008 else original(a, n)
        self.assertTrue(scope.presented(0xA000, ids[0xA000]))

    def test_live_unreadable_link_is_not_silently_treated_as_absent_ui(self):
        scope, g, ids, _, _ = self.fixture()
        original = g.read
        def read(a, n):
            if a==0x8000+0x28:raise OSError('unreadable live slot')
            return original(a, n)
        g.read = read
        with self.assertRaises(OSError):
            scope.presented(0xA000, ids[0xA000])

    def test_native_owner_getter_change_is_rejected(self):
        scope, g, ids, _, _ = self.fixture()
        g.put(0xB000+0x2E0, struct.pack('<Q', g.base+1))
        with self.assertRaisesRegex(RuntimeError, 'getter changed'):
            scope.presented(0xA000, ids[0xA000])

    def test_parent_chain_cycle_is_bounded(self):
        scope, _, ids, _, link = self.fixture()
        link(0x8000, 'Parent', 0x9000)
        with self.assertRaisesRegex(RuntimeError, 'Cyclic'):
            scope.presented(0xA000, ids[0xA000])

    def test_changed_link_during_snapshot_defers_presentation(self):
        scope, g, ids, _, _ = self.fixture()
        original = g.read
        reads = [0]
        def read(a, n):
            if a==0x8000+0x28:
                reads[0] += 1
                if reads[0]>1:return bytes(8)
            return original(a, n)
        g.read = read
        with self.assertRaises(MenuPresentationChanging):
            scope.presented(0xA000, ids[0xA000])

    def test_implausible_viewport_map_is_rejected_before_reading_nodes(self):
        scope, g, ids, _, _ = self.fixture()
        g.put(0xC068, struct.pack('<ii', 5000, 5000))
        with self.assertRaisesRegex(RuntimeError, 'Invalid local UI viewport map'):
            scope.presented(0xA000, ids[0xA000])

    def test_new_displayed_widget_is_returned_without_global_object_rescan(self):
        scope, _, _, _, link = self.fixture()
        link(0x9000, 'DisplayedWidget', 0x11000)
        self.assertEqual(scope.begin(), (0x11000,))


if __name__ == '__main__':
    unittest.main()
