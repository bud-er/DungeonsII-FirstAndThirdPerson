from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from click_move_guard import ClickMoveGuard, NATIVE_GUARDS, MASK, TREE_PACKAGE, OBJECT_ARRAY_RVA
from test_native_ranged_facing import fixture as ranged_fixture


def fixture():
    f = ranged_fixture('ST_PlayerInput')
    s, g = f.s, f.g
    f.player, f.package, f.path = 0xC0000, 0x90000, 0xD0000
    for obj, cls, name, outer in ((s.pc, 'GameplayPlayerController', 'PC', f.package),
            (s.pawn, 'PlayerCharacter', 'Pawn', f.package),
            (f.player, 'LocalPlayer', 'LocalPlayer', f.package),
            (f.package, 'Package', TREE_PACKAGE, 0),
            (f.path, 'PlayerPathFollowingComponent', 'PathFollowing', s.pc)):
        f.ids[obj] = obj
        f.headers[obj] = dict(address=obj, **{'class': cls}, name=name, outer=outer, flags=0)
    f.headers[f.component].update(address=f.component, name='InputStateMachine')
    f.headers[f.tree].update(address=f.tree)
    s.reflection.objects = lambda: iter(list(f.headers.values()))
    s.pointer = lambda obj, cls, name: {
        'Player': f.player, 'Pawn': s.pawn, 'Controller': s.pc,
        'StateTreeComponent': s.component, 'PathFollowingComponent': f.path}[name]
    s.native['STT_PlayerPath'] = 0xE0000
    props = s.layouts['CompactStateTreeState']['properties']
    props.extend(dict(name=n, offset=o, size=z) for n, o, z in (
        ('ChildrenBegin', 0x2C, 2), ('ChildrenEnd', 0x2E, 2),
        ('EnterConditionsBegin', 0x30, 2), ('EnterConditionsNum', 0x53, 1),
        ('SelectionBehavior', 0x5B, 1)))
    props.append(dict(name='bEnabled', offset=0x5D, size=1,
        bool=dict(fieldSize=1, byteOffset=0, byteMask=MASK, fieldMask=MASK)))
    s.layouts['PlayerPathFollowingComponent'] = dict(size=0x320, properties=[])
    s.layouts['StateTree']['properties'].append(dict(name='Transitions', offset=0x60, size=16))
    s.layouts['CompactStateTransition'] = dict(size=0x30, properties=[dict(name=n,offset=o,size=z) for n,o,z in
        (('RequiredEvent',0,16),('State',0x22,2),('Trigger',0x28,1))])
    s.layouts['CompactEventDesc'] = dict(size=0x10, properties=[dict(name='Tag',offset=8,size=8)])
    names = ('PrimaryActionRelease', 'InteractionRelease', 'Interaction',
             'MeleeAttackRelease', 'Attack', 'PathTowardsCursor', 'Empty')
    s.reflection.name = lambda index, number: 'SW.StateTree.Event.StopPrimaryAction' if index==50 else names[index - 1]
    rows = bytearray(49 * 0x60)
    details = ((0,77,0,21,0), (16,77,1,21,1), (17,78,0,22,0),
               (16,78,1,22,1), (19,79,0,23,0), (16,79,3,23,2), (16,82,0,25,0))
    for i, (parent, cond, cond_count, task, task_count) in enumerate(details):
        offset = (16 + i) * 0x60
        struct.pack_into('<II', rows, offset + 0x10, i + 1, 0)
        struct.pack_into('<H', rows, offset + 0x2A, parent)
        struct.pack_into('<H', rows, offset + 0x30, cond)
        struct.pack_into('<H', rows, offset + 0x36, task)
        rows[offset + 0x53], rows[offset + 0x56] = cond_count, task_count
        rows[offset + 0x5B], rows[offset + 0x5D] = 1, 0x70
    struct.pack_into('<HH', rows, 16 * 0x60 + 0x2C, 17, 23)
    g.put(f.tree + 0x50, struct.pack('<Qii', f.states, 49, 52))
    g.put(f.states, rows)
    size, count = 0xC00, 90
    g.put(f.tree + 0x70, struct.pack('<Qii', f.nodes, size, count))
    g.put(f.nodes + size - 24 * 16, struct.pack('<Q', s.native['STT_PlayerPath']))
    f.transitions = 0x300000
    transitions = bytearray(69 * 0x30)
    for index, state in ((4,16),(8,16),(21,21)):
        struct.pack_into('<II', transitions, index * 0x30 + 8, 50, 0)
        struct.pack_into('<H', transitions, index * 0x30 + 0x22, state)
        transitions[index * 0x30 + 0x28] = 8
    g.put(f.transitions, transitions)
    g.put(f.tree + 0x60, struct.pack('<Qii', f.transitions, 69, 69))
    g.put(f.path + 0x24C, b'\0')
    for rva, expected in NATIVE_GUARDS.items(): g.put(g.base + rva, expected)
    f.writes = []
    def write(address, value):
        f.writes.append((address, value))
        g.put(address, value)
    g.write = write
    f.address = f.states + 21 * 0x60 + 0x5D
    class_ids = {}
    s.is_a = lambda cls, name: class_ids.get(cls, cls) == name
    def publish_objects():
        pointers, chunk = 0xF0000, 0x100000
        descriptor = bytearray(0x30)
        struct.pack_into('<Q', descriptor, 0x10, pointers)
        struct.pack_into('<IIII', descriptor, 0x20, 65536, len(f.headers), 1, 1)
        g.put(g.base + OBJECT_ARRAY_RVA, descriptor)
        g.put(pointers, struct.pack('<Q', chunk))
        for index, (address, header) in enumerate(f.headers.items()):
            header['index'] = index
            class_id = next((key for key, value in class_ids.items() if value == header['class']), None)
            if class_id is None:
                class_id = 0x200000 + len(class_ids) * 0x100
                class_ids[class_id] = header['class']
            g.put(address + 0x10, struct.pack('<Q', class_id))
            item = bytearray(24); struct.pack_into('<Q', item, 0, address)
            struct.pack_into('<I', item, 0x10, f.ids.get(address, 99))
            g.put(chunk + index * 24, item)
    f.publish_objects = publish_objects
    publish_objects()
    return f


def add_remote(f):
    owner, player, component, storage, frames = 0x310000,0x320000,0x330000,0x340000,0x350000
    for address, cls, name, outer in ((owner,'GameplayPlayerController','RemotePC',f.package),
            (player,'NetConnection','RemoteConnection',f.package),
            (component,'PlayerStateTreeComponent','InputStateMachine',owner)):
        f.headers[address] = dict(address=address, **{'class':cls}, name=name,outer=outer,flags=0)
        f.ids[address] = address
    base_pointer = f.s.pointer
    f.remote_player, f.remote_input = player, 0
    f.s.pointer = lambda obj, cls, name: ({'StateTreeComponent':component,'Player':f.remote_player,
        'PlayerInput':f.remote_input}[name] if obj==owner else base_pointer(obj,cls,name))
    f.g.put(component+0x100,struct.pack('<Q',f.tree))
    f.g.put(component+0xD8,struct.pack('<QQ',storage,storage-16))
    f.g.put(storage+0x10,struct.pack('<Qii',frames,1,4))
    frame=bytearray(0x68);struct.pack_into('<Q',frame,0,f.tree);frame[0x3C]=1
    f.g.put(frames,frame)
    f.publish_objects()
    return owner,player,component,frames


class ClickMoveGuardTests(unittest.TestCase):
    def test_constructor_is_read_only_and_only_target_bit_changes(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        self.assertEqual(f.writes, [])
        original = f.g.read(f.states, 49 * 0x60)
        self.assertFalse(p.update())
        self.assertTrue(p.update())
        expected = bytearray(original); expected[21 * 0x60 + 0x5D] &= ~MASK
        self.assertEqual(f.g.read(f.states, 49 * 0x60), bytes(expected))
        self.assertEqual(f.writes, [(f.address, b'\x50')])
        p.restore(); self.assertEqual(f.g.read(f.states, 49 * 0x60), original)
        self.assertFalse(p.applied); self.assertFalse(p.restore_failures)

    def test_shift_keeps_empty_ground_path_suppressed_and_interaction_rows_intact(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        interaction = f.g.read(f.states + 17 * 0x60, 4 * 0x60)
        self.assertFalse(p.update(interact=True))
        self.assertTrue(p.update(interact=True))
        self.assertEqual(f.g.read(f.address, 1), b'\x50')
        self.assertEqual(f.g.read(f.states + 17 * 0x60, 4 * 0x60), interaction)

    def test_active_click_state_or_nonidle_path_defers_without_writes(self):
        for active, status in ((21, 0), (0, 1), (0, 2), (0, 3), (0, 255)):
            with self.subTest(active=active, status=status):
                f = fixture(); p = ClickMoveGuard(f.s)
                f.g.put(f.frames + 0x2C, struct.pack('<H', active))
                f.g.put(f.path + 0x24C, bytes([status]))
                self.assertFalse(p.update()); self.assertEqual(f.writes, [])
                f.g.put(f.frames + 0x2C, struct.pack('<H', 0)); f.g.put(f.path + 0x24C, b'\0')
                self.assertFalse(p.update()); self.assertTrue(p.update())

    def test_status_transition_during_idle_snapshot_defers(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        def change(address, size):
            if address == f.frames:
                f.g.on_read = None; f.g.put(f.path + 0x24C, b'\x03')
        f.g.on_read = change
        self.assertFalse(p.update()); self.assertEqual(f.writes, [])

    def test_foreign_component_and_multiple_consumers_refused(self):
        for field in ('component', 'player'):
            f = fixture(); obj = getattr(f, field); extra = obj + 0x100
            f.headers[extra] = dict(f.headers[obj], address=extra)
            f.publish_objects()
            with self.assertRaisesRegex(RuntimeError, '[Mm]ultiple'): ClickMoveGuard(f.s)
            self.assertEqual(f.writes, [])
        f = fixture(); f.headers[f.component]['outer'] = f.s.pawn
        with self.assertRaisesRegex(RuntimeError, 'consumer|owned'): ClickMoveGuard(f.s)

    def test_native_instruction_and_layout_and_branch_guards(self):
        cases = (
            lambda f: f.g.put(f.g.base + next(iter(NATIVE_GUARDS)), b'\x90'),
            lambda f: f.s.layouts['CompactStateTreeState'].update(size=0x61),
            lambda f: f.g.put(f.states + 21 * 0x60 + 0x2A, struct.pack('<H', 19)),
            lambda f: f.g.put(f.states + 22 * 0x60 + 0x5D, b'\0'),
            lambda f: f.headers[f.package].update(name='/Game/Unexpected'),
            lambda f: f.g.put(f.nodes + 0xC00 - 24 * 16, struct.pack('<Q', 0xBAD00)),
        )
        for mutate in cases:
            f = fixture(); mutate(f)
            with self.assertRaises(RuntimeError): ClickMoveGuard(f.s)
            self.assertEqual(f.writes, [])

    def test_restore_does_not_require_old_controller_eligibility(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        f.s.pointer = lambda *args: (_ for _ in ()).throw(AssertionError('must not inspect lost controller'))
        p.restore(); self.assertFalse(p.restore_failures)
        self.assertEqual(f.g.read(f.address, 1), b'\x70')

    def test_lost_scope_restores_asset_before_error(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        f.s.component += 8
        with self.assertRaisesRegex(RuntimeError, 'ownership'): p.update()
        self.assertEqual(f.g.read(f.address, 1), b'\x70'); self.assertFalse(p.applied)

    def test_restore_preserves_other_bits_changed_by_native_game(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        f.g.put(f.address, b'\x90'); p.restore()
        self.assertEqual(f.g.read(f.address, 1), b'\xb0')

    def test_restore_preserves_other_bits_changed_during_validation(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        reads = []
        def change(address, size):
            if address == p.row_start:
                reads.append(address)
                if len(reads) == 2:
                    f.g.on_read = None; f.g.put(f.address, b'\x90')
        f.g.on_read = change; p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual(f.g.read(f.address, 1), b'\xb0')

    def test_retired_tree_never_dereferenced_and_rebuilt_buffer_never_written(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update(); f.writes.clear()
        f.s.object_retired = lambda obj, identity: obj == f.tree
        original = f.g.read
        f.g.read = lambda address, size: (_ for _ in ()).throw(AssertionError('retired dereference')) if address == f.tree + 0x50 else original(address,size)
        p.restore(); self.assertFalse(p.restore_failures); self.assertEqual(f.writes, [])
        f = fixture(); p = ClickMoveGuard(f.s); p.update(); f.writes.clear()
        f.g.put(f.tree + 0x50, struct.pack('<Qii', f.states + 0x10000, 49, 52))
        p.restore(); self.assertTrue(p.restore_failures); self.assertTrue(p.applied); self.assertEqual(f.writes, [])

    def test_live_unreadable_asset_and_restore_failure_remain_blockers(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        original = f.g.read
        f.g.read = lambda address,size: (_ for _ in ()).throw(OSError('unreadable live tree')) if address == f.tree + 0x50 else original(address,size)
        p.restore(); self.assertTrue(p.applied); self.assertTrue(p.restore_failures)
        f.g.read = original; p.restore(); self.assertFalse(p.applied)

    def test_package_retirement_alone_cannot_claim_live_tree_is_restored(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update(); f.writes.clear()
        f.s.object_retired = lambda obj, identity: obj == f.package
        p.restore()
        self.assertTrue(p.applied); self.assertTrue(p.restore_failures)
        self.assertEqual(f.writes, [])

    def test_rebuilt_array_copy_retains_restoration_blocker(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update(); f.writes.clear()
        replacement = f.states + 0x10000
        f.g.put(replacement, f.g.read(f.states, 49 * 0x60))
        f.g.put(f.tree + 0x50, struct.pack('<Qii', replacement, 49, 52))
        p.restore()
        self.assertTrue(p.applied); self.assertTrue(p.restore_failures)
        self.assertEqual(f.writes, [])
        self.assertEqual(f.g.read(replacement + 21 * 0x60 + 0x5D, 1), b'\x50')

    def test_inflight_path_waits_after_suppression_then_latches_readiness(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        self.assertFalse(p.update()); self.assertTrue(p.applied)
        f.g.put(f.path + 0x24C, b'\x03')
        self.assertFalse(p.update())
        f.g.put(f.path + 0x24C, b'\0')
        self.assertTrue(p.update())
        f.g.put(f.path + 0x24C, b'\x03')
        self.assertTrue(p.update(interact=True))

    def test_large_reserved_object_capacity_is_supported(self):
        f = fixture()
        f.g.put(f.g.base + OBJECT_ARRAY_RVA + 0x20, struct.pack('<I', 2162688))
        ClickMoveGuard(f.s)

    def test_partial_write_failure_is_recorded_and_retryable(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        def write(address, value):
            self.assertTrue(p.applied)
            f.g.put(address, value)
            raise OSError('write result uncertain')
        f.g.write = write
        with self.assertRaisesRegex(RuntimeError, 'restoration'): p.update()
        self.assertTrue(p.restore_failures); self.assertTrue(p.applied)
        f.g.write = lambda address,value: f.g.put(address,value)
        p.restore(); self.assertFalse(p.restore_failures); self.assertFalse(p.applied)

    def test_new_extra_consumer_restores_during_bounded_audit(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        extra = f.component + 0x100; f.headers[extra] = dict(f.headers[f.component], address=extra)
        f.publish_objects()
        p.next_audit = 0.
        with self.assertRaisesRegex(RuntimeError, '[Mm]ultiple'): p.update()
        self.assertEqual(f.g.read(f.address, 1), b'\x70')

    def test_periodic_audit_consumes_only_bounded_slice(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        original = list(f.headers.values()); reads = []
        def objects():
            for _ in range(p.AUDIT_SLICE * 2):
                reads.append(1); yield dict(address=0xFF0000, **{'class':'Unrelated'}, name='Other')
            yield from original
        p.objects_iter = objects; p.next_audit = 0.
        self.assertFalse(p.update()); self.assertEqual(len(reads), p.AUDIT_SLICE)

    def test_asset_rebuild_during_idle_snapshot_never_writes_stale_buffer(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        def change(address, size):
            if address == f.frames:
                f.g.on_read = None
                f.g.put(f.tree + 0x50, struct.pack('<Qii', f.states + 0x10000, 49, 52))
        f.g.on_read = change
        with self.assertRaisesRegex(RuntimeError, 'allocation changed'): p.update()
        self.assertEqual(f.writes, [])

    def test_asset_rebuild_during_row_read_never_writes_stale_buffer(self):
        f = fixture(); p = ClickMoveGuard(f.s)
        def change(address, size):
            if address == p.row_start:
                f.g.on_read = None
                f.g.put(f.tree + 0x50, struct.pack('<Qii', f.states + 0x10000, 49, 52))
        f.g.on_read = change
        with self.assertRaisesRegex(RuntimeError, 'allocation changed'): p.update()
        self.assertEqual(f.writes, [])

    def test_live_copied_rebuild_during_restore_row_read_is_not_retirement(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update(); f.writes.clear()
        replacement = f.states + 0x10000
        def change(address, size):
            if address == p.row_start:
                f.g.on_read = None
                f.g.put(replacement, f.g.read(f.states, 49 * 0x60))
                f.g.put(f.tree + 0x50, struct.pack('<Qii', replacement, 49, 52))
        f.g.on_read = change
        p.restore()
        self.assertTrue(p.applied); self.assertTrue(p.restore_failures)
        self.assertEqual(f.writes, [])

    def test_recycled_cached_header_cannot_hide_new_consumer(self):
        f = fixture(); p = ClickMoveGuard(f.s); p.update()
        extra = f.component + 0x100
        f.headers[extra] = dict(f.headers[f.component], address=extra)
        f.s.reflection.headers[extra] = dict(address=extra, **{'class':'Unrelated'}, name='Old')
        fresh = f.s.reflection.header
        f.s.reflection.header = lambda obj: f.s.reflection.headers.get(obj) or fresh(obj)
        f.publish_objects(); p.next_audit = 0.
        with self.assertRaisesRegex(RuntimeError, '[Mm]ultiple'): p.update()
        self.assertEqual(f.g.read(f.address, 1), b'\x70')

    def test_verified_remote_network_consumer_allows_host_with_local_only_writes(self):
        f=fixture(); owner,player,component,frames=add_remote(f);p=ClickMoveGuard(f.s)
        self.assertFalse(p.update());self.assertTrue(p.update())
        self.assertEqual(set(address for address,value in f.writes),{f.address})
        p.restore();self.assertFalse(p.restore_failures)

    def test_remote_owner_null_player_or_player_input_is_refused(self):
        for problem in ('null','local','input','owner'):
            f=fixture();owner,player,component,frames=add_remote(f)
            if problem=='null':f.remote_player=0
            if problem=='local':f.remote_player=f.player
            if problem=='input':f.remote_input=0xDEAD00
            if problem=='owner':f.headers[owner]['class']='UnknownController';f.publish_objects()
            with self.assertRaises(RuntimeError):ClickMoveGuard(f.s)
            self.assertEqual(f.writes,[])

    def test_remote_active_release_branch_is_refused(self):
        for index in (14,16,17,18,19,20,21,22):
            f=fixture();owner,player,component,frames=add_remote(f)
            f.g.put(frames+0x2C,struct.pack('<H',index))
            with self.assertRaisesRegex(RuntimeError,'active click-release'):ClickMoveGuard(f.s)

    def test_remote_gain_of_input_is_detected_without_waiting_for_global_audit(self):
        f=fixture();add_remote(f);p=ClickMoveGuard(f.s);p.update();p.update()
        p.next_audit=float('inf');f.remote_input=0xDEAD00
        with self.assertRaisesRegex(RuntimeError,'ambiguous input'):p.update()
        self.assertEqual(f.g.read(f.address,1),b'\x70')

    def test_unknown_release_producer_in_asset_is_rejected(self):
        f=fixture();f.g.put(f.transitions+4*0x30+8,struct.pack('<II',1,0))
        with self.assertRaisesRegex(RuntimeError,'unguarded event'):ClickMoveGuard(f.s)

    def test_remote_producer_native_guard_cannot_be_bypassed(self):
        f=fixture();add_remote(f);f.g.put(f.g.base+0x63B42F2,b'\x90')
        with self.assertRaisesRegex(RuntimeError,'instruction'):ClickMoveGuard(f.s)


if __name__ == '__main__': unittest.main()
