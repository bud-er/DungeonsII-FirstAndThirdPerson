import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from forward_targeting import ForwardTargeting, NATIVE_GUARDS, STRIDE, in_front
from inspect_camera import OBJECT_ARRAY_RVA


class Memory:
    base=0x100000

    def __init__(self):
        self.bytes={}
        self.writes=[]

    def load(self,address,data):
        self.bytes.update((address+i,value) for i,value in enumerate(data))

    def read(self,address,size):
        return bytes(self.bytes.get(address+i,0) for i in range(size))

    def write(self,address,data):
        self.writes.append((address,data))
        self.load(address,data)

    def u32(self,address):return struct.unpack('<I',self.read(address,4))[0]
    def u64(self,address):return struct.unpack('<Q',self.read(address,8))[0]


class ForwardTargetingTests(unittest.TestCase):
    def profile(self):
        g=Memory()
        s=SimpleNamespace(game=g,pc=20000,root=50000,yaw=0.,identities={30000:1})
        s.identity=Mock(return_value=1)
        s.ensure_layout=Mock()
        s.is_a=lambda cls,name: (name=='MobCharacter' and cls in (91,92)) or (name=='PlayerCharacter' and cls==92)
        s.pointer=lambda actor,cls,name:30000 if name=='AutoTargeter' else actor+0x1000
        for rva,data in NATIVE_GUARDS.items():g.load(g.base+rva,data)
        g.load(g.base+OBJECT_ARRAY_RVA+0x24,struct.pack('<I',20))
        g.load(g.base+OBJECT_ARRAY_RVA+0x10,struct.pack('<Q',70000))
        g.load(70000,struct.pack('<Q',80000))
        g.load(s.root+0x200,struct.pack('<ddd',0.,0.,0.))
        f=ForwardTargeting(s,30000)
        self.install_rows(f,[])
        return f

    def actor(self,f,index,position,cls=91,serial=11,flags=0):
        actor=100000+index*0x2000
        item=bytearray(24)
        struct.pack_into('<Q',item,0,actor)
        struct.pack_into('<i',item,0x10,serial)
        f.game.load(80000+index*24,item)
        header=bytearray(0x28)
        struct.pack_into('<IIQ',header,8,flags,index,cls)
        f.game.load(actor,header)
        f.game.load(actor+0x1000+0x200,struct.pack('<ddd',*position))
        return struct.pack('<ii',index,serial)

    def install_rows(self,f,rows,pointer=90000):
        descriptor=struct.pack('<Qii',pointer,len(rows),len(rows)+5)
        f.game.load(f.header,descriptor)
        for i,(flags,weak,config) in enumerate(rows):
            raw=bytearray(STRIDE)
            raw[0]=flags
            raw[4:12]=weak
            struct.pack_into('<Q',raw,0x30,config)
            f.game.load(pointer+i*STRIDE,raw)
        return descriptor

    def test_cone_uses_camera_yaw_and_ignores_height(self):
        self.assertTrue(in_front((0,0,0),(100,0,900),0))
        self.assertFalse(in_front((0,0,0),(-100,0,0),0))
        self.assertFalse(in_front((0,0,0),(0,100,0),0))
        self.assertTrue(in_front((0,0,0),(0,100,0),90))
        self.assertTrue(in_front((0,0,0),(0,0,900),180))

    def test_rear_mob_loses_only_ranged_bit_and_turning_restores_it(self):
        f=self.profile()
        front=self.actor(f,1,(100,0,0));rear=self.actor(f,2,(-100,0,0))
        self.install_rows(f,[(5,front,200),(13,rear,200)])
        f.game.load(f.targeter+0x18C,b'pickup!!')
        f.game.load(f.targeter+0xB8,b'\x01')
        f.update()
        self.assertEqual(f.game.read(90000,1),b'\x05')
        self.assertEqual(f.game.read(90000+STRIDE,1),b'\x09')
        self.assertEqual(f.game.writes,[(90000+STRIDE,b'\x09')])
        f.session.yaw=180;f.update()
        self.assertEqual(f.game.read(90000,1),b'\x01')
        self.assertEqual(f.game.read(90000+STRIDE,1),b'\x0d')
        self.assertEqual(f.game.read(f.targeter+0x18C,8),b'pickup!!')
        self.assertEqual(f.game.read(f.targeter+0xB8,1),b'\x01')

    def test_items_other_players_and_native_melee_only_mobs_are_untouched(self):
        f=self.profile()
        item=self.actor(f,1,(-100,0,0),cls=90)
        player=self.actor(f,2,(-100,0,0),cls=92)
        melee=self.actor(f,3,(-100,0,0))
        self.install_rows(f,[(5,item,200),(5,player,200),(1,melee,300)])
        f.update();f.restore()
        self.assertEqual(f.game.writes,[])

    def test_cached_rear_mob_is_cleared_but_never_restored(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)])
        f.game.load(f.targeter+0x154,rear)
        f.update();f.game.writes.clear();f.restore()
        self.assertEqual(f.game.read(f.targeter+0x154,8),bytes(8))
        self.assertEqual(f.game.writes,[(90000,b'\x05')])
        self.assertFalse(f.restore_failures)

    def test_cached_front_mob_remains_selected(self):
        f=self.profile();front=self.actor(f,1,(100,0,0))
        f.game.load(f.targeter+0x154,front)
        f.update()
        self.assertEqual(f.game.writes,[])

    def test_recycled_weak_serial_and_garbage_objects_are_not_written(self):
        f=self.profile()
        old=self.actor(f,1,(-100,0,0),serial=11)
        self.actor(f,1,(-100,0,0),serial=12)
        garbage=self.actor(f,2,(-100,0,0),flags=0x40000000)
        self.install_rows(f,[(5,old,200),(5,garbage,200)])
        f.game.load(f.targeter+0x154,old)
        f.update()
        self.assertEqual(f.game.writes,[])

    def test_reordered_and_retired_candidates_restore_only_current_rows(self):
        f=self.profile()
        rear=self.actor(f,1,(-100,0,0));retired=self.actor(f,2,(-100,0,0))
        self.install_rows(f,[(5,rear,200),(5,retired,200)]);f.update()
        newcomer=self.actor(f,3,(100,0,0))
        self.install_rows(f,[(1,newcomer,300),(9,rear,200)],pointer=95000)
        f.game.writes.clear();f.restore()
        self.assertEqual(f.game.writes,[(95000+STRIDE,b'\x0d')])
        self.assertFalse(f.disabled);self.assertFalse(f.restore_failures)

    def test_changed_native_candidate_kind_is_not_given_ranged_eligibility(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)]);f.update()
        self.install_rows(f,[(1,rear,300)])
        f.game.writes.clear();f.session.yaw=180;f.update();f.restore()
        self.assertEqual(f.game.writes,[])
        self.assertFalse(f.disabled)

    def test_array_reallocation_and_row_replacement_abort_write(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        descriptor=self.install_rows(f,[(5,rear,200)])
        config=struct.pack('<Q',200)
        self.install_rows(f,[(5,rear,200)],pointer=95000)
        self.assertFalse(f.write_flag(descriptor,90000,rear,config,False))
        descriptor=self.install_rows(f,[(5,rear,200)])
        f.game.load(90004,struct.pack('<ii',2,11))
        self.assertFalse(f.write_flag(descriptor,90000,rear,config,False))
        self.assertEqual(f.game.writes,[])

    def test_restore_keeps_pending_records_when_array_changes(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)]);f.update()
        f.write_flag=Mock(return_value=False);f.game.writes.clear();f.restore()
        self.assertEqual(f.restore_failures,{'ranged candidates'})
        self.assertTrue(f.disabled);self.assertEqual(f.game.writes,[])

    def test_replaced_owner_blocks_update_and_restoration(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)]);f.update()
        f.session.identity.return_value=2;f.game.writes.clear()
        with self.assertRaises(RuntimeError):f.update()
        f.restore()
        self.assertEqual(f.game.writes,[])
        self.assertTrue(f.restore_failures)

    def test_invalid_origin_is_rejected_before_any_write(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)])
        f.game.load(f.session.root+0x200,struct.pack('<ddd',math.nan,0.,0.))
        with self.assertRaises(RuntimeError):f.update()
        self.assertEqual(f.game.writes,[])

    def test_retiring_root_during_snapshot_does_not_disable_camera(self):
        f=self.profile();rear=self.actor(f,1,(-100,0,0))
        self.install_rows(f,[(5,rear,200)])
        f.position=Mock(side_effect=OSError('root retired'))
        f.update()
        self.assertEqual(f.game.writes,[])

    def test_invalid_array_and_native_guard_are_rejected(self):
        f=self.profile()
        f.game.load(f.header,struct.pack('<Qii',90000,10,5))
        with self.assertRaises(RuntimeError):f.update()
        f.game.load(f.game.base+next(iter(NATIVE_GUARDS)),b'\x00')
        with self.assertRaises(RuntimeError):ForwardTargeting(f.session,f.targeter)
        self.assertEqual(f.game.writes,[])


if __name__=='__main__':unittest.main()
