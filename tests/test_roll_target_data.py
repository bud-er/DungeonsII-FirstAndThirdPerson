"""Emulate the production leaf instructions; no live process or writes."""
import contextlib
import io
import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from roll_target_data import (vector_stub, roll_vector, RollTargetDataHook,
                              PATCHES, GUARDS, FORWARD_GETTER, SEQUENCE, VECTOR)
from test_native_roll_hook import Backend, OFFSETS
from test_roll_direction import Memory

try:
    import unicorn
    import unicorn.x86_const as x86
except ImportError:
    unicorn = None


class RollVectorTests(unittest.TestCase):
    def test_camera_relative_forward_backward_left_right_and_diagonal(self):
        for yaw in (-179., -90., 0., 38., 179.):
            for axes, offset in (((0,1),0), ((0,-1),180), ((1,0),90),
                                 ((-1,0),-90), ((1,1),45), ((0,0),0)):
                v = struct.unpack('<ddd', roll_vector(yaw,axes))
                a = math.radians(yaw+offset)
                self.assertAlmostEqual(v[0], math.cos(a))
                self.assertAlmostEqual(v[1], math.sin(a))
                self.assertEqual(v[2], 0.)

    def test_invalid_input_rejected(self):
        for yaw, axes in ((float('nan'),(0,0)), (0,(2,0)), (0,(0,float('inf')))):
            with self.assertRaises(RuntimeError): roll_vector(yaw, axes)

    def test_exact_calls_resolve_to_native_pawn_forward_getter(self):
        for rva, code in PATCHES.items():
            self.assertEqual(len(code), 5)
            self.assertEqual(rva+5+struct.unpack('<i',code[1:])[0], FORWARD_GETTER)


@unittest.skipIf(unicorn is None, 'Unicorn x64 emulator required for a live roll trial')
class RollTargetDataInstructions(unittest.TestCase):
    def emulate(self, change=None, overlap=False):
        u = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_64)
        code,cfg,original,end = 0x200000,0x201000,0x202000,0x203000
        pc,pawn,player,root,serials,out = [0x300000+i*0x1000 for i in range(6)]
        stack = 0x400000
        u.mem_map(code,0x4000); u.mem_map(pc,0x6000); u.mem_map(stack,0x10000)
        offsets = dict(OFFSETS,pawn_root=0x1B8)
        raw = bytearray(176)
        struct.pack_into('<I',raw,0,1)
        struct.pack_into('<QQIIQQIIQ',raw,8,pc,pawn,3,4,serials,serials+4,11,22,player)
        struct.pack_into('<QII',raw,80,serials+8,33,5)
        struct.pack_into('<QIIQI',raw,96,root,6,0,serials+12,44)
        vector = roll_vector(30.,(0.,-1.))
        raw[VECTOR:VECTOR+24] = vector
        u.mem_write(cfg,bytes(raw))
        u.mem_write(serials,struct.pack('<IIII',11,22,33,44))
        for obj,index in ((pc,3),(pawn,4),(player,5),(root,6)):
            u.mem_write(obj+0xC,struct.pack('<I',index))
        for obj,offset,value in ((pc,offsets['pc_pawn'],pawn),(pc,offsets['pc_player'],player),
                                 (pawn,offsets['pawn_pc'],pc),(pawn,0x1B8,root)):
            u.mem_write(obj+offset,struct.pack('<Q',value))
        u.mem_write(pawn+offsets['role'],b'\x02')
        # Foreign path's ordinary native getter returns a recognizable vector.
        native = b'\x48\xb8'+struct.pack('<Q',cfg+176)
        native += bytes.fromhex('0f1000f20f1048100f1102f20f114a10488bc2c3')
        u.mem_write(cfg+176,struct.pack('<ddd',1.,2.,0.))
        u.mem_write(original,native)
        regs = ('RAX','RBX','RCX','RDX','RSI','RDI','RBP','R8','R9','R10','R11','R12','R13','R14','R15')
        initial = {name:0xABC000+i for i,name in enumerate(regs)}
        initial.update(RCX=root,RDX=out,RDI=pawn,R14=pc)
        for name,value in initial.items():u.reg_write(getattr(x86,'UC_X86_REG_'+name),value)
        sp=stack+0xEFF8
        u.reg_write(x86.UC_X86_REG_RSP,sp);u.mem_write(sp,struct.pack('<Q',end))
        u.mem_write(sp-32,b'B'*32);u.mem_write(sp+8,b'A'*40)
        for i in range(16):u.reg_write(getattr(x86,'UC_X86_REG_XMM'+str(i)),0xABC000+i)
        if change:change(u,cfg,pc,pawn,player,root,serials)
        initial={name:u.reg_read(getattr(x86,'UC_X86_REG_'+name)) for name in regs}
        def step(cpu,address,size,data):
            self.assertEqual(cpu.reg_read(x86.UC_X86_REG_RSP),sp)
            if overlap and bytes(cpu.mem_read(address,size)) == bytes.fromhex('443b9888000000'):
                cpu.mem_write(cfg+SEQUENCE,struct.pack('<I',2))
        u.hook_add(unicorn.UC_HOOK_CODE,step)
        u.mem_write(code,vector_stub(code,cfg,original,offsets))
        u.emu_start(code,end,count=1000)
        self.assertEqual(u.reg_read(x86.UC_X86_REG_RSP),sp+8)
        self.assertEqual(u.reg_read(x86.UC_X86_REG_RAX),out)
        self.assertEqual(bytes(u.mem_read(sp-32,32)),b'B'*32)
        self.assertEqual(bytes(u.mem_read(sp+8,40)),b'A'*40)
        for name in ('RBX','RSI','RDI','RBP','R12','R13','R14','R15'):
            self.assertEqual(u.reg_read(getattr(x86,'UC_X86_REG_'+name)),initial[name])
        for i in range(6,16):self.assertEqual(u.reg_read(getattr(x86,'UC_X86_REG_XMM'+str(i))),0xABC000+i)
        return bytes(u.mem_read(out,24)),struct.unpack('<II',u.mem_read(cfg+128,8)),vector

    def test_local_backward_vector_returned_without_stack_or_nonvolatile_changes(self):
        actual,counters,expected=self.emulate()
        self.assertEqual(actual,expected);self.assertEqual(counters,(1,0))

    def test_inactive_foreign_serial_possession_role_root_and_odd_sequence_use_native(self):
        changes = (
            lambda u,c,*_:u.mem_write(c,bytes(4)),
            lambda u,c,p,*_:u.reg_write(x86.UC_X86_REG_R14,0xBAD),
            lambda u,c,p,w,l,r,s:u.mem_write(s+4,struct.pack('<I',99)),
            lambda u,c,p,w,l,r,s:u.mem_write(p+OFFSETS['pc_pawn'],bytes(8)),
            lambda u,c,p,w,l,r,s:u.mem_write(w+OFFSETS['role'],b'\x01'),
            lambda u,c,p,w,l,r,s:u.reg_write(x86.UC_X86_REG_RCX,0xBAD),
            lambda u,c,p,w,l,r,s:u.mem_write(w+0x1B8,bytes(8)),
            lambda u,c,p,w,l,r,s:u.mem_write(s+12,struct.pack('<I',99)),
            lambda u,c,*_:u.mem_write(c+SEQUENCE,struct.pack('<I',1)),
        )
        for change in changes:
            actual,counters,_=self.emulate(change)
            self.assertEqual(struct.unpack('<ddd',actual),(1.,2.,0.))
            self.assertEqual(counters,(0,1))

    def test_publication_overlapping_read_falls_back_to_native(self):
        actual,counters,_=self.emulate(overlap=True)
        self.assertEqual(struct.unpack('<ddd',actual),(1.,2.,0.))
        self.assertEqual(counters,(0,1))


class RollTargetDataTransactions(unittest.TestCase):
    def setUp(self):
        self.available=patch('roll_target_data.ROLL_TARGET_DATA_AVAILABLE',True)
        self.available.start();self.addCleanup(self.available.stop)
    def make(self):
        g,b=Memory(),Backend()
        g.u32=lambda a:struct.unpack('<I',g.read(a,4))[0]
        for rva,data in {**PATCHES,**GUARDS}.items():g.load(g.base+rva,data)
        fields={'Pawn':('pc_pawn','ObjectProperty',8),'Player':('pc_player','ObjectProperty',8),
                'Controller':('pawn_pc','ObjectProperty',8),'Role':('role','ByteProperty',1),
                'RootComponent':('pawn_root','ObjectProperty',8)}
        offsets=dict(OFFSETS,pawn_root=0x1B8)
        s=SimpleNamespace(game=g,pc=100,pawn=200,root=300,local_scope=Mock())
        s.field=lambda obj,cls,p:dict(offset=offsets[fields[p][0]],type=fields[p][1],size=fields[p][2])
        return RollTargetDataHook(g,b),s

    def test_two_sites_install_restore_without_touching_failed_experiment(self):
        h,s=self.make()
        with contextlib.redirect_stdout(io.StringIO()):h.prepare(s);h.restore()
        self.assertFalse(h.installed);self.assertFalse(h.restore_failures)
        for rva,data in {**PATCHES,**GUARDS}.items():self.assertEqual(h.game.read(h.game.base+rva,len(data)),data)
        self.assertFalse(any(e[0]=='free' for e in h.backend.events))
        self.assertEqual(sum(e[0]=='suspend' for e in h.backend.events),sum(e[0]=='resume' for e in h.backend.events))

    def test_changed_native_or_old_prototype_sites_refuse_allocation_and_writes(self):
        for rva in (next(iter(PATCHES)),0x5AAD599,0x5A997FB):
            h,s=self.make();h.game.load(h.game.base+rva,bytes(5))
            with self.assertRaises(RuntimeError):h.prepare(s)
            self.assertFalse(h.game.writes);self.assertFalse(h.backend.events)

    def test_partial_instruction_write_rolls_back(self):
        h,s=self.make()
        target=h.game.base+list(PATCHES)[1]
        original=h.game.write
        def write(address,data):
            if address==target and data!=PATCHES[list(PATCHES)[1]]:
                original(address,data[:2])
                raise OSError('simulated partial instruction write')
            original(address,data)
        h.game.write=write
        with self.assertRaises(OSError):h.prepare(s)
        for rva,data in PATCHES.items():self.assertEqual(h.game.read(h.game.base+rva,5),data)
        self.assertFalse(h.pending_restores)

    def test_publish_vector_uses_odd_then_even_sequence_and_rejects_nonunit(self):
        h,s=self.make()
        with contextlib.redirect_stdout(io.StringIO()):h.prepare(s)
        h.game.writes.clear();h.publish_vector(roll_vector(0,(0,-1)))
        self.assertEqual([a for a,v in h.game.writes],[h.config+SEQUENCE,h.config+VECTOR,h.config+SEQUENCE])
        self.assertEqual(h.game.u32(h.config+SEQUENCE),2)
        with self.assertRaises(RuntimeError):h.publish_vector(bytes(24))


class RollTargetDataRefusal(unittest.TestCase):
    def test_failed_live_trial_blocks_allocation_thread_pause_and_writes(self):
        g,backend=Mock(),Mock()
        h=RollTargetDataHook(g,backend)
        with self.assertRaisesRegex(RuntimeError,'disabled'):h.prepare(Mock())
        g.write.assert_not_called();backend.allocate_near.assert_not_called();backend.frozen.assert_not_called()


if __name__=='__main__':unittest.main()
