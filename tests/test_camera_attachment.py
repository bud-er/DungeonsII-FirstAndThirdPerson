from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_attachment import CameraAttachment


class Memory:
    def __init__(self):
        self.data = bytearray(0x10000)
        self.writes = []
        self.fail = None

    def read(self,address,size):
        return bytes(self.data[address:address+size])

    def write(self,address,data):
        if self.fail and self.fail(address,data):
            raise OSError('simulated write failure')
        self.writes.append((address,data))
        self.data[address:address+len(data)] = data

    def put(self,address,fmt,*values):
        data=struct.pack(fmt,*values)
        self.data[address:address+len(data)] = data

    def u64(self,address):
        return struct.unpack('<Q',self.read(address,8))[0]


class CameraAttachmentTests(unittest.TestCase):
    def make(self,capacity=8):
        g=Memory()
        s=SimpleNamespace(game=g,camera=0x1000,arm=0x2000,root=0x3000,pawn=0x4000)
        s.identities={s.camera:1,s.arm:1,s.root:1}
        s.identity=lambda obj:s.identities[obj]
        s.ensure_layout=lambda _:None
        offsets={'AttachParent':0xC8,'AttachSocketName':0xD0,'AttachChildren':0xE0,'bReplicates':0x88}
        s.field=lambda obj,cls,name:dict(address=obj+offsets[name],bool={'byteMask':1})
        for obj in (s.camera,s.arm,s.root):g.put(obj+0xA0,'<Q',s.pawn)
        g.put(s.camera+0xC8,'<Q',s.arm)
        g.put(s.arm+0xE0,'<Qii',0x6000,1,4)
        g.put(0x6000,'<Q',s.camera)
        g.put(s.root+0xE0,'<Qii',0x7000,2,capacity)
        g.put(0x7000,'<QQ',0x8000,0x9000)
        return s,g

    def test_attach_and_restore_reciprocal_membership(self):
        s,g=self.make();a=CameraAttachment(s)
        a.activate()
        self.assertEqual(g.u64(s.camera+0xC8),s.root)
        self.assertEqual(a.children(s.arm)[1],[])
        self.assertEqual(a.children(s.root)[1],[0x8000,0x9000,s.camera])
        a.restore()
        self.assertEqual(g.u64(s.camera+0xC8),s.arm)
        self.assertEqual(a.children(s.arm)[1],[s.camera])
        self.assertEqual(a.children(s.root)[1],[0x8000,0x9000])
        self.assertFalse(a.applied)

    def test_full_destination_refuses_before_any_write(self):
        s,g=self.make(capacity=2)
        with self.assertRaisesRegex(RuntimeError,'spare capacity'):CameraAttachment(s)
        self.assertFalse(g.writes)

    def test_replicated_parent_refused(self):
        s,g=self.make();g.put(s.root+0x88,'B',1)
        with self.assertRaisesRegex(RuntimeError,'non-replicated'):CameraAttachment(s)
        self.assertFalse(g.writes)

    def test_different_actor_owner_refused(self):
        s,g=self.make();g.put(s.root+0xA0,'Q',0x5000)
        with self.assertRaisesRegex(RuntimeError,'owner'):CameraAttachment(s)

    def test_restore_preserves_new_children_and_resized_array(self):
        s,g=self.make();a=CameraAttachment(s);a.activate()
        g.put(s.root+0xE0,'<Qii',0xA000,5,8)
        g.put(0xA000,'<5Q',0x8000,0x9000,s.camera,0xB000,0xC000)
        a.restore()
        self.assertEqual(a.children(s.root)[1],[0x8000,0x9000,0xB000,0xC000])
        self.assertEqual(g.u64(s.root+0xE0),0xA000)

    def test_partial_activation_is_cleaned_up(self):
        for fail_address in (0x7010,0x30E8,0x10C8,0x20E8):
            with self.subTest(address=hex(fail_address)):
                s,g=self.make();a=CameraAttachment(s)
                g.fail=lambda address,data:address==fail_address
                with self.assertRaises(OSError):a.activate()
                g.fail=None;a.restore()
                self.assertEqual(g.u64(s.camera+0xC8),s.arm)
                self.assertEqual(a.children(s.arm)[1],[s.camera])
                self.assertEqual(a.children(s.root)[1],[0x8000,0x9000])

    def test_failed_removal_count_can_recover_without_losing_new_children(self):
        s,g=self.make();a=CameraAttachment(s);a.activate()
        g.put(0x7018,'<QQ',0xB000,0xC000);g.put(s.root+0xE8,'<i',5)
        g.fail=lambda address,data:address==s.root+0xE8
        with self.assertRaises(OSError):a.restore()
        self.assertTrue(a.applied)
        g.fail=None;a.restore()
        self.assertEqual(a.children(s.root)[1],[0x8000,0x9000,0xB000,0xC000])
        self.assertFalse(a.applied)

    def test_recycled_camera_is_never_restored_into(self):
        s,g=self.make();a=CameraAttachment(s);a.activate();g.writes.clear()
        s.identity=lambda obj:2 if obj==s.camera else 1
        with self.assertRaisesRegex(RuntimeError,'object changed'):a.restore()
        self.assertFalse(g.writes)

    def test_foreign_native_reparent_is_not_overwritten(self):
        s,g=self.make();a=CameraAttachment(s);a.activate();g.writes.clear()
        g.put(s.camera+0xC8,'<Q',0x5000)
        with self.assertRaisesRegex(RuntimeError,'Game changed'):a.restore()
        self.assertFalse(g.writes)


if __name__=='__main__':unittest.main()
