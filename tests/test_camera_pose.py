import contextlib
import io
import math
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_pose import CameraPose, compose, rotator_quaternion
from external_camera import CameraSession


class Memory:
    def __init__(self):
        self.data = bytearray(0x10000)
        self.writes = []

    def read(self,address,size):
        return bytes(self.data[address:address+size])

    def write(self,address,data):
        self.writes.append((address,data))
        self.data[address:address+len(data)] = data

    def put(self,address,fmt,*values):
        data = struct.pack(fmt,*values)
        self.data[address:address+len(data)] = data


class ZoneCamera(unittest.TestCase):
    def make_session(self):
        s = CameraSession.__new__(CameraSession)
        s.game = g = Memory()
        s.camera,s.arm,s.root,s.pawn,s.pc = 0x1000,0x2000,0x3000,0x4000,0x5000
        s.height,s.fov,s.yaw,s.pitch = 55,115,30,-10
        s.identities = {s.camera:1,s.arm:1,s.root:1,s.pawn:1,s.pc:1}
        s.identity = Mock(return_value=1)
        s.pointer = Mock(side_effect=lambda obj,cls,name: s.pawn if name=='Pawn' else s.arm if name=='AttachParent' else s.camera)
        s.controller_profile = s.avatar_profile = s.damage_feedback = None
        s.blend,s.blend_started = 1,None
        s.originals,s.plan = {},[]
        p = s.pose = CameraPose.__new__(CameraPose)
        p.session,p.game = s,g
        p.location,p.rotation = s.camera+0x140,s.camera+0x158
        p.additive_flags,p.additive_fov,p.native_fov_address = s.camera+0x282,s.camera+0x2F4,s.camera+0x240
        p.relative_q,p.relative_pos = (0,0,0,1),(0,0,0)
        p.attachment = Mock()
        p.last_relative_pos = p.last_blend = None
        for address,size,mask in ((p.location,24,None),(p.rotation,24,None),
                                  (s.camera+0x1A0,1,2),(p.additive_flags,1,0x10),(p.additive_fov,4,None)):
            s.save(s.camera,address,size,mask)
        s.plan = [(s.camera+0x1A0,2,2),(p.additive_flags,0x10,0x10)]
        g.put(s.root+0x1E0,'<12d',0,0,0,1,100,200,300,0,1,1,1,0)
        g.put(p.native_fov_address,'<f',45)
        self.set_zone(s,3700,-45,45)
        return s,g

    def set_zone(self,s,length,pitch,yaw):
        g=s.game
        g.put(s.arm+0x1E0,'<12d',*rotator_quaternion(pitch,yaw),100,200,210,0,1,1,1,0)
        g.put(s.arm+0x300,'<ddd',-length,0,0)
        g.put(s.arm+0x320,'<dddd',0,0,0,1)
        g.put(s.arm+0x240,'<f',length)
        g.put(s.arm+0x340,'<f',3700)

    def test_crossing_zones_never_writes_the_native_arm_or_fov(self):
        s,g=self.make_session()
        for distance,pitch in ((3700,-45),(6000,-30),(1800,-50)):
            self.set_zone(s,distance,pitch,45)
            before=g.read(s.arm,0x360)
            s.update()
            self.assertEqual(g.read(s.arm,0x360),before)
            self.assertEqual(struct.unpack('<ddd',g.read(s.camera+0x200,24)),(100,200,355))
            self.assertEqual(struct.unpack('<f',g.read(s.pose.native_fov_address,4)),(45,))
            self.assertFalse(any(s.arm<=a<s.arm+0x360 for a,_ in g.writes))

    def test_restore_uses_destination_zone_after_native_zoom_and_rotation_change(self):
        s,g=self.make_session()
        s.update()
        self.set_zone(s,1600,-30,75)
        expected=s.pose.native_pose()
        with contextlib.redirect_stdout(io.StringIO()):s.restore()
        self.assertFalse(s.restore_failures)
        actual=struct.unpack('<12d',g.read(s.camera+0x1E0,0x60))
        self.assertEqual(actual[:4],expected[0])
        self.assertEqual(actual[4:7],expected[1])
        self.assertEqual(g.read(s.pose.location,24),bytes(24))
        self.assertFalse(g.read(s.pose.additive_flags,1)[0]&0x10)
        self.assertEqual(struct.unpack('<f',g.read(s.arm+0x240,4)),(1600,))

    def test_zoom_out_transition_targets_current_zone(self):
        s,g=self.make_session()
        s.blend=.5
        self.set_zone(s,2200,-20,10)
        native_q,native_pos=s.pose.native_pose()
        s.update()
        actual=struct.unpack('<ddd',g.read(s.camera+0x200,24))
        for a,b,c in zip(actual,native_pos,(100,200,355)):self.assertAlmostEqual(a,(b+c)/2)

    def test_native_fov_changes_remain_intact(self):
        s,g=self.make_session()
        s.update()
        g.put(s.pose.native_fov_address,'<f',60)
        s.update()
        self.assertEqual(struct.unpack('<f',g.read(s.pose.additive_fov,4)),(55,))
        with contextlib.redirect_stdout(io.StringIO()):s.restore()
        self.assertEqual(struct.unpack('<f',g.read(s.pose.native_fov_address,4)),(60,))

    def test_engine_moves_attached_camera_between_helper_updates(self):
        s,g=self.make_session()
        s.update()
        # Movement and a zone zoom/rotation occur before another helper update.
        g.put(s.root+0x200,'<ddd',150,220,300)
        self.set_zone(s,6000,-15,100)
        relative=struct.unpack('<ddd',g.read(s.pose.location,24))
        _,actual=compose((0,0,0,1),(150,220,300),(0,0,0,1),relative)
        for a,b in zip(actual,(150,220,355)):self.assertAlmostEqual(a,b)

    def test_steady_first_person_neither_reads_zoom_pose_nor_rewinds_world_position(self):
        s,g=self.make_session()
        s.update()
        s.pose.native_pose = Mock(side_effect=AssertionError('Zoom arm must not position first person'))
        g.put(s.root+0x200,'<ddd',130,240,300)
        g.put(s.camera+0x200,'<ddd',130,240,355)  # Native attachment propagation.
        g.writes.clear()
        s.update()
        self.assertNotIn(s.camera+0x200,[a for a,_ in g.writes])
        self.assertEqual(struct.unpack('<ddd',g.read(s.camera+0x200,24)),(130,240,355))

    def test_failed_reattachment_keeps_camera_fields_for_retry(self):
        s,g=self.make_session()
        s.update()
        s.pose.attachment.restore.side_effect = OSError('temporary failure')
        g.writes.clear()
        with contextlib.redirect_stdout(io.StringIO()):s.restore()
        self.assertIn('camera attachment',s.restore_failures)
        self.assertFalse(g.writes)

    def test_recycled_camera_is_not_written_on_restore(self):
        s,g=self.make_session()
        s.identity=Mock(return_value=2)
        with contextlib.redirect_stdout(io.StringIO()):s.restore()
        self.assertFalse(g.writes)
        self.assertTrue(s.restore_failures)

    def test_parent_rotation_transforms_socket_position(self):
        q,pos=compose(rotator_quaternion(0,90),(10,20,30),(0,0,0,1),(-100,0,0))
        for a,b in zip(pos,(10,-80,30)):self.assertAlmostEqual(a,b)
        self.assertAlmostEqual(sum(v*v for v in q),1)


if __name__=='__main__':unittest.main()
