"""Instruction emulation and reversible patch transactions; never accesses a game."""
import contextlib
import io
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from native_roll_hook import (NativeRollHook, NativeRollProfile, PATCHES, GUARDS,
                              relative_branch, direction_stub, stick_stub, PAGE, BOOL_GETTER)
from test_roll_direction import Memory

try:
    import unicorn
    import unicorn.x86_const as x86
except ImportError:
    unicorn = None


OFFSETS = {'pc_pawn': 0x300, 'pc_player': 0x420, 'pawn_pc': 0x2D8, 'role': 0x170}


class NativeRollBuildGuards(unittest.TestCase):
    def test_both_direction_calls_resolve_to_the_verified_getter(self):
        calls = [(rva, code) for rva, code in PATCHES.items() if code[0] == 0xE8]
        self.assertEqual(len(calls), 2)
        for rva, code in calls:
            self.assertEqual(rva+5+struct.unpack('<i', code[1:])[0], BOOL_GETTER)

    def test_every_redirect_is_inside_a_busy_thread_exclusion(self):
        hook = NativeRollHook.__new__(NativeRollHook)
        hook.game = SimpleNamespace(base=0x100000)
        hook.allocation = None
        for rva in PATCHES:
            for offset in range(5):
                self.assertFalse(hook._safe_rips([hook.game.base+rva+offset]))


@unittest.skipIf(unicorn is None, 'Optional Unicorn x64 emulator is not installed')
class NativeRollInstructions(unittest.TestCase):
    def emulate(self, stick=False, change=None, original_result=0, flags=0x246):
        u = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_64)
        code, cfg, original, end = 0x200000, 0x201000, 0x203000, 0x204000
        pc, pawn, player, serials, stack = 0x300000, 0x301000, 0x302000, 0x303000, 0x400000
        u.mem_map(code, 0x5000)
        u.mem_map(pc, 0x4000)
        u.mem_map(stack, 0x10000)
        u.mem_write(original, bytes([0xB0, original_result, 0xC3]))
        raw = bytearray(96)
        struct.pack_into('<I', raw, 0, 1)
        struct.pack_into('<QQIIQQIIQ', raw, 8, pc, pawn, 3, 4,
                         serials, serials+4, 11, 22, player)
        struct.pack_into('<QII', raw, 80, serials+8, 33, 5)
        u.mem_write(cfg, bytes(raw))
        for obj, index in ((pc, 3), (pawn, 4), (player, 5)):
            u.mem_write(obj+0xC, struct.pack('<I', index))
        u.mem_write(serials, struct.pack('<III', 11, 22, 33))
        u.mem_write(pc+OFFSETS['pc_pawn'], struct.pack('<Q', pawn))
        u.mem_write(pc+OFFSETS['pc_player'], struct.pack('<Q', player))
        u.mem_write(pawn+OFFSETS['pawn_pc'], struct.pack('<Q', pc))
        u.mem_write(pawn+OFFSETS['role'], b'\x02')
        regs = ('RAX', 'RBX', 'RCX', 'RDX', 'RSI', 'RDI', 'RBP',
                'R8', 'R9', 'R10', 'R11', 'R12', 'R13', 'R14', 'R15')
        initial = {name: 0x1111000000+i*0x1234 for i, name in enumerate(regs)}
        initial['RDI'] = pc if stick else pawn
        initial['RSI' if stick else 'R14'] = pawn if stick else pc
        for name, value in initial.items():
            u.reg_write(getattr(x86, 'UC_X86_REG_'+name), value)
        u.reg_write(x86.UC_X86_REG_EFLAGS, flags)
        sp = stack+0xEFF8
        u.reg_write(x86.UC_X86_REG_RSP, sp)
        u.mem_write(sp, struct.pack('<Q', end))
        u.mem_write(sp-32, b'B'*32)
        u.mem_write(sp+40, b'A'*32)
        # Leaf unwind must find the return address at the current RSP at
        # every instruction, including the foreign tail-call path.
        u.hook_add(unicorn.UC_HOOK_CODE, lambda cpu, address, size, data:
                   self.assertEqual(cpu.reg_read(x86.UC_X86_REG_RSP), sp))
        initial_xmm = {}
        for i in range(16):
            value = 0x10000000000000000000000000000000+i
            u.reg_write(getattr(x86, 'UC_X86_REG_XMM'+str(i)), value)
            initial_xmm[i] = value
        if change:
            change(u, cfg, pc, pawn, player, serials)
            initial = {name: u.reg_read(getattr(x86, 'UC_X86_REG_'+name)) for name in regs}
        native = stick_stub(code, cfg, OFFSETS) if stick else direction_stub(code, cfg, original, OFFSETS)
        u.mem_write(code, native)
        u.emu_start(code, end, count=10000)
        output = {name: u.reg_read(getattr(x86, 'UC_X86_REG_'+name)) for name in regs}
        self.assertEqual(u.reg_read(x86.UC_X86_REG_RSP), sp+8)
        self.assertEqual(u.reg_read(x86.UC_X86_REG_EFLAGS), flags)
        self.assertEqual(bytes(u.mem_read(sp-32, 32)), b'B'*32)
        self.assertEqual(bytes(u.mem_read(sp+40, 32)), b'A'*32)
        for i, value in initial_xmm.items():
            self.assertEqual(u.reg_read(getattr(x86, 'UC_X86_REG_XMM'+str(i))), value)
        for name in regs:
            if name == ('RDX' if stick else 'RAX'):
                continue
            self.assertEqual(output[name], initial[name], name)
        if not stick:
            self.assertEqual(output['RAX'] & ~0xFF, initial['RAX'] & ~0xFF)
        return output['RDX'] if stick else output['RAX'] & 0xFF, bytes(u.mem_read(cfg+64, 12))

    def test_local_guest_chooses_directional_and_left_stick_without_clobbering_abi(self):
        value, counters = self.emulate()
        self.assertEqual(value, 1)
        self.assertEqual(struct.unpack('<III', counters), (1, 0, 0))
        value, counters = self.emulate(stick=True)
        self.assertEqual(value, 0)
        self.assertEqual(struct.unpack('<III', counters), (0, 1, 0))

    def test_local_authority_uses_the_same_direction_route(self):
        def authority(u, cfg, pc, pawn, player, serials):
            u.mem_write(pawn+OFFSETS['role'], b'\x03')
        self.assertEqual(self.emulate(change=authority)[0], 1)
        self.assertEqual(self.emulate(stick=True, change=authority)[0], 0)

    def test_every_arithmetic_flag_combination_survives_local_and_foreign_leaf_stubs(self):
        bits = (0, 2, 4, 6, 7, 11)
        for combination in range(64):
            flags = 0x202 | sum(1 << bit for index, bit in enumerate(bits) if combination & (1 << index))
            for stick in (False, True):
                for active in (False, True):
                    def gate(u, cfg, *args):
                        u.mem_write(cfg, struct.pack('<I', int(active)))
                    with self.subTest(flags=flags, stick=stick, active=active):
                        self.emulate(stick=stick, change=gate, flags=flags)

    def test_inactive_foreign_recycled_and_unpossessed_calls_keep_native_behavior(self):
        def write(where, value):
            return lambda u, cfg, pc, pawn, player, serials: u.mem_write(
                where(cfg, pc, pawn, player, serials), value)
        changes = {
            'inactive': write(lambda c,p,a,l,s: c, bytes(4)),
            'pc index': write(lambda c,p,a,l,s: p+0xC, struct.pack('<I', 99)),
            'pawn index': write(lambda c,p,a,l,s: a+0xC, struct.pack('<I', 99)),
            'player index': write(lambda c,p,a,l,s: l+0xC, struct.pack('<I', 99)),
            'pc serial': write(lambda c,p,a,l,s: s, struct.pack('<I', 99)),
            'pawn serial': write(lambda c,p,a,l,s: s+4, struct.pack('<I', 99)),
            'player serial': write(lambda c,p,a,l,s: s+8, struct.pack('<I', 99)),
            'possession': write(lambda c,p,a,l,s: p+OFFSETS['pc_pawn'], bytes(8)),
            'controller': write(lambda c,p,a,l,s: a+OFFSETS['pawn_pc'], bytes(8)),
            'local player': write(lambda c,p,a,l,s: p+OFFSETS['pc_player'], bytes(8)),
            'simulated': write(lambda c,p,a,l,s: a+OFFSETS['role'], b'\x01'),
            'unknown role': write(lambda c,p,a,l,s: a+OFFSETS['role'], b'\x04'),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                value, counters = self.emulate(change=change)
                self.assertEqual(value, 0)
                self.assertEqual(struct.unpack('<III', counters), (0, 0, 1))
                self.assertEqual(self.emulate(stick=True, change=change)[0], 1)
                self.assertEqual(self.emulate(change=change, original_result=1)[0], 1)

    def test_foreign_pointer_is_refused_before_any_object_dereference(self):
        for stick in (False, True):
            def foreign(u, *args):
                u.reg_write(x86.UC_X86_REG_RDI if stick else x86.UC_X86_REG_R14, 0xBAD)
            self.assertEqual(self.emulate(stick=stick, change=foreign)[0], int(stick))


class Backend:
    def __init__(self):
        self.events, self.rips = [], []
    def allocate_near(self):
        return 0x7000000
    def protect(self, address, size, value):
        self.events.append(('protect', address, value))
        return 0x20
    def flush(self, address, size):
        self.events.append(('flush', address))
    def free_unpublished(self, address):
        self.events.append(('free', address))
    @contextlib.contextmanager
    def frozen(self):
        self.events.append(('suspend',))
        try:
            yield self.rips
        finally:
            self.events.append(('resume',))


class NativeRollTransactions(unittest.TestCase):
    def profile(self):
        g, backend = Memory(), Backend()
        for rva, data in {**PATCHES, **GUARDS}.items():
            g.load(g.base+rva, data)
        fields = {'Pawn': ('pc_pawn', 'ObjectProperty', 8),
                  'Player': ('pc_player', 'ObjectProperty', 8),
                  'Controller': ('pawn_pc', 'ObjectProperty', 8),
                  'Role': ('role', 'ByteProperty', 1)}
        s = SimpleNamespace(game=g, pc=100, pawn=200, local_scope=Mock())
        s.field = lambda obj, cls, prop: {'offset': OFFSETS[fields[prop][0]],
                                          'type': fields[prop][1], 'size': fields[prop][2]}
        return NativeRollHook(g, backend), s

    def prepare(self, hook, session):
        # Only the fake byte-addressed memory backend may exercise the quarantined
        # transaction logic. Production entry points retain the disabled guard.
        with patch('native_roll_hook.NATIVE_ROLL_AVAILABLE', True), contextlib.redirect_stdout(io.StringIO()):
            hook.prepare(session)

    def test_failed_live_trial_blocks_allocation_pause_and_patch_writes(self):
        h, s = self.profile()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            h.prepare(s)
        self.assertEqual(h.game.writes, [])
        self.assertEqual(h.backend.events, [])

    def restore(self, hook):
        with contextlib.redirect_stdout(io.StringIO()):
            hook.restore()

    def test_three_fixed_width_sites_install_flush_restore_and_retain_published_stubs(self):
        h, s = self.profile()
        self.prepare(h, s)
        self.assertTrue(h.installed)
        self.assertEqual(h.game.read(h.config, 4), bytes(4))
        for rva, data in PATCHES.items():
            self.assertEqual(len(h.desired[rva]), 5)
            self.assertEqual(h.game.read(h.game.base+rva, 5), h.desired[rva])
        self.restore(h)
        self.assertFalse(h.installed)
        self.assertFalse(h.restore_failures)
        for rva, data in PATCHES.items():
            self.assertEqual(h.game.read(h.game.base+rva, 5), data)
        self.assertFalse(any(e[0] == 'free' for e in h.backend.events))
        self.assertEqual(sum(e[0]=='suspend' for e in h.backend.events),
                         sum(e[0]=='resume' for e in h.backend.events))

    def test_mismatched_code_and_bad_ownership_layout_block_all_writes(self):
        for change in ('code', 'layout'):
            h, s = self.profile()
            if change == 'code':
                h.game.load(h.game.base+next(iter(PATCHES)), b'other')
            else:
                s.field = lambda *args: {'offset': 4, 'type': 'ObjectProperty', 'size': 8}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.prepare(h, s)
            self.assertEqual(h.game.writes, [])

    def test_partial_patch_write_rolls_back_with_threads_resumed(self):
        h, s = self.profile()
        real_write = h.game.write
        fail = h.game.base+list(PATCHES)[1]
        attempted = []
        def partial(address, data):
            if address == fail and not attempted:
                attempted.append(True)
                h.game.load(address, data[:2])
                raise OSError('partial write')
            real_write(address, data)
        h.game.write = partial
        with self.assertRaisesRegex(OSError, 'partial write'):
            self.prepare(h, s)
        self.assertFalse(h.installed)
        for rva, data in PATCHES.items():
            self.assertEqual(h.game.read(h.game.base+rva, 5), data)
        self.assertEqual(h.backend.events[-1], ('resume',))

    def test_changed_foreign_patch_is_never_overwritten_during_restore(self):
        h, s = self.profile()
        self.prepare(h, s)
        address = h.game.base+list(PATCHES)[1]
        h.game.load(address, b'other')
        h.game.writes.clear()
        self.restore(h)
        self.assertTrue(h.restore_failures)
        self.assertEqual(h.game.read(address, 5), b'other')
        self.assertEqual(h.game.writes, [(h.config, bytes(4))])

    def interrupt_write_and_rollback(self, hook, rva):
        original_write = hook.game.write
        address = hook.game.base+rva
        failures = []
        def interrupted(where, data):
            if where == address and len(failures) < 2:
                size = (3, 2)[len(failures)]
                failures.append(True)
                before = hook.game.read(where, len(data))
                hook.game.load(where, data[:size]+before[size:])
                raise OSError('interrupted write')
            original_write(where, data)
        hook.game.write = interrupted
        return address

    def test_f5_recovers_interrupted_installation_and_rollback(self):
        h, s = self.profile()
        rva = next(iter(PATCHES))
        address = self.interrupt_write_and_rollback(h, rva)
        with self.assertRaisesRegex(RuntimeError, 'rollback was incomplete'):
            self.prepare(h, s)
        self.assertNotIn(h.game.read(address, 5), (PATCHES[rva], h.desired[rva]))
        self.assertIn(rva, h.pending_restores)
        with self.assertRaisesRegex(RuntimeError, 'restoration is pending'):
            self.prepare(h, s)
        self.restore(h)
        self.assertFalse(h.restore_failures)
        self.assertFalse(h.pending_restores)
        self.assertFalse(h.installed)
        for site, original in PATCHES.items():
            self.assertEqual(h.game.read(h.game.base+site, 5), original)
        self.assertEqual(h.backend.events[-1], ('resume',))
        self.prepare(h, s)
        self.assertTrue(h.installed)
        self.restore(h)
        self.assertFalse(h.restore_failures)

    def test_f5_recovers_interrupted_restoration_and_rollback(self):
        h, s = self.profile()
        self.prepare(h, s)
        rva = list(PATCHES)[1]
        address = self.interrupt_write_and_rollback(h, rva)
        self.restore(h)
        self.assertTrue(h.restore_failures)
        self.assertNotIn(h.game.read(address, 5), (PATCHES[rva], h.desired[rva]))
        self.restore(h)
        self.assertFalse(h.restore_failures)
        self.assertFalse(h.pending_restores)
        for site, original in PATCHES.items():
            self.assertEqual(h.game.read(h.game.base+site, 5), original)

    def test_recovery_refuses_foreign_bytes_and_changed_surrounding_guards(self):
        for change in ('site', 'guard'):
            h, s = self.profile()
            rva = next(iter(PATCHES))
            address = self.interrupt_write_and_rollback(h, rva)
            with self.assertRaises(RuntimeError):
                self.prepare(h, s)
            foreign = address if change == 'site' else h.game.base+next(iter(GUARDS))
            h.game.load(foreign, b'other')
            h.game.writes.clear()
            with self.subTest(change=change):
                self.restore(h)
                self.assertTrue(h.restore_failures)
                self.assertEqual(h.game.read(foreign, 5), b'other')
                self.assertEqual(h.game.writes, [(h.config, bytes(4))])

    def test_rollback_never_claims_or_overwrites_unrelated_failure_bytes(self):
        h, s = self.profile()
        rva = next(iter(PATCHES))
        address = h.game.base+rva
        original_write = h.game.write
        def foreign(where, data):
            if where == address:
                h.game.load(where, b'other')
                raise OSError('external code changed')
            original_write(where, data)
        h.game.write = foreign
        with self.assertRaisesRegex(RuntimeError, 'rollback was incomplete'):
            self.prepare(h, s)
        self.assertEqual(h.game.read(address, 5), b'other')
        self.assertNotIn(b'other', h.pending_restores[rva]['bytes'])
        h.game.writes.clear()
        self.restore(h)
        self.assertTrue(h.restore_failures)
        self.assertEqual(h.game.writes, [(h.config, bytes(4))])

    def test_recovery_restores_original_page_protection_after_two_protect_failures(self):
        h, s = self.profile()
        rva = list(PATCHES)[-1]  # This site occupies a different native page.
        address = h.game.base+rva
        protections, failures = {}, []
        def protect(where, size, value):
            page = where & ~(PAGE-1)
            before = protections.get(page, 0x20)
            if where == address and value == 0x20 and len(failures) < 2:
                failures.append(True)
                raise OSError('protection restore failed')
            protections[page] = value
            return before
        h.backend.protect = protect
        with self.assertRaisesRegex(RuntimeError, 'rollback was incomplete'):
            self.prepare(h, s)
        self.assertEqual(protections[address & ~(PAGE-1)], 0x40)
        self.assertEqual(h.pending_restores[rva]['protection'], 0x20)
        self.restore(h)
        self.assertFalse(h.restore_failures)
        self.assertFalse(h.pending_restores)
        self.assertTrue(all(protections[(h.game.base+rva) & ~(PAGE-1)] == 0x20
                            for rva in PATCHES))

    def test_restored_stub_allocation_is_reused_on_next_session(self):
        h, s = self.profile()
        self.prepare(h, s)
        allocation = h.allocation
        self.restore(h)
        h.backend.allocate_near = Mock(side_effect=AssertionError('must reuse published code'))
        self.prepare(h, s)
        self.assertTrue(h.installed)
        self.assertEqual(h.allocation, allocation)
        self.restore(h)
        self.assertFalse(h.restore_failures)

    def test_busy_native_thread_prevents_patch_writes_and_always_resumes(self):
        h, s = self.profile()
        h.backend.rips = [h.game.base+list(PATCHES)[0]]
        with patch('native_roll_hook.time.monotonic', side_effect=[0., 2.]), self.assertRaisesRegex(RuntimeError, 'remained busy'):
            self.prepare(h, s)
        self.assertFalse(h.installed)
        self.assertEqual(h.backend.events[-1], ('resume',))
        self.assertFalse(any(address == h.game.base+rva for address, _ in h.game.writes for rva in PATCHES))

    def test_mixed_owned_sites_can_be_restored_without_overwriting_foreign_code(self):
        h, s = self.profile()
        self.prepare(h, s)
        rva, original = next(iter(PATCHES.items()))
        h.game.load(h.game.base+rva, original)
        self.restore(h)
        self.assertFalse(h.restore_failures)
        self.assertFalse(h.installed)

    def test_relative_call_range_is_checked(self):
        self.assertEqual(relative_branch(0xE8, 1000, 2000), b'\xe8'+struct.pack('<i', 995))
        with self.assertRaises(RuntimeError):
            relative_branch(0xE8, 1, 1<<40)

    def test_profile_deactivates_after_focus_loss_and_on_partial_activation_failure(self):
        manager = Mock()
        session = SimpleNamespace(game=SimpleNamespace(native_roll_hook=manager), local_scope=Mock())
        p = NativeRollProfile(session)
        p.update()
        manager.activate.assert_called_once_with(session)
        p.set_input(False)
        p.update()
        self.assertFalse(p.applied)
        manager.deactivate.assert_called_once()
        manager.activate.side_effect = RuntimeError('activation failed')
        p.set_input(True)
        with self.assertRaises(RuntimeError):
            p.update()
        self.assertTrue(p.applied)
        p.restore()
        self.assertFalse(p.applied)


if __name__ == '__main__':
    unittest.main()
