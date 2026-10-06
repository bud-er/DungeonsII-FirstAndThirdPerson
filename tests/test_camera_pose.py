import contextlib
import io
import math
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_pose import CameraPose, compose, rotator_quaternion, rotate
from camera_modes import Perspective
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
    def make_session(self, additive=(0.,0.,0.)):
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
        p.additive_location = s.camera+0x2B0
        g.put(p.additive_location,'<ddd',*additive)
        p.relative_q,p.relative_pos = (0,0,0,1),(0,0,0)
        p.attachment = Mock()
        p.last_relative_pos = p.last_additive_pos = p.last_blend = None
        for address,size,mask in ((p.location,24,None),(p.rotation,24,None),
                                  (s.camera+0x1A0,1,2),(p.additive_flags,1,0x10),
                                  (p.additive_location,24,None),(p.additive_fov,4,None)):
            s.save(s.camera,address,size,mask)
        s.plan = [(s.camera+0x1A0,2,2),(p.additive_flags,0x10,0x10)]
        g.put(s.root+0x1E0,'<12d',0,0,0,1,100,200,300,0,1,1,1,0)
        g.put(p.native_fov_address,'<f',45)
        self.set_zone(s,3700,-45,45)
        return s,g

    @staticmethod
    def rendered_position(s):
        # Native GetCameraView rotates additive translation by the component's
        # absolute rotation, then adds its current world translation.
        g = s.game
        q = struct.unpack('<dddd',g.read(s.camera+0x1E0,32))
        pos = struct.unpack('<ddd',g.read(s.camera+0x200,24))
        local = struct.unpack('<ddd',g.read(s.pose.additive_location,24))
        return tuple(a+b for a,b in zip(pos,rotate(q,local)))

    @staticmethod
    def propagate_root(s, yaw, pos):
        # Model native attachment propagation between external helper ticks.
        # bAbsoluteRotation leaves the camera quaternion unchanged.
        g = s.game
        q = rotator_quaternion(0.,yaw)
        g.put(s.root+0x1E0,'<12d',*q,*pos,0,1,1,1,0)
        relative = struct.unpack('<ddd',g.read(s.pose.location,24))
        g.put(s.camera+0x200,'<ddd',*(a+b for a,b in zip(pos,rotate(q,relative))))

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

    def test_shoulder_view_does_not_swing_with_native_body_facing_between_ticks(self):
        s,g = self.make_session()
        s.perspective = Perspective(third=True)
        s.update()
        expected_offset = s.perspective.offset(s.yaw,s.pitch,s.height)
        for body_yaw in (90.,-90.,180.,22.):
            self.propagate_root(s,body_yaw,(140.,240.,300.))
            for actual,anchor,offset in zip(self.rendered_position(s),(140.,240.,300.),expected_offset):
                self.assertAlmostEqual(actual,anchor+offset)

    def test_mouse_turning_never_rewinds_native_moving_shoulder_anchor(self):
        s,g = self.make_session()
        s.perspective = Perspective(third=True)
        s.update()
        s.pose.native_pose = Mock(side_effect=AssertionError('No zoom-arm reads in custom view'))
        for yaw,pitch in ((90.,0.),(135.,45.),(-179.,-65.),(30.,-10.)):
            self.propagate_root(s,-90.,(150.,220.,300.))
            # Root has advanced after the camera propagated. An external world
            # write would now produce a distinct extra movement step.
            g.put(s.root+0x200,'<ddd',155.,225.,300.)
            s.yaw,s.pitch = yaw,pitch
            g.writes.clear()
            s.update()
            self.assertNotIn(s.camera+0x200,[a for a,_ in g.writes])
            self.assertNotIn(s.pose.location,[a for a,_ in g.writes])
            expected_offset = s.perspective.offset(yaw,pitch,s.height)
            for actual,anchor,offset in zip(self.rendered_position(s),(150.,220.,300.),expected_offset):
                self.assertAlmostEqual(actual,anchor+offset)

    def test_additive_shoulder_fade_preserves_interpolated_final_view(self):
        for blend in (0.,.15,.5,.85,1.):
            for amount in (0.,.3,1.):
                with self.subTest(blend=blend,amount=amount):
                    s,g = self.make_session()
                    s.blend = blend
                    s.perspective = Perspective(third=True)
                    s.perspective.amount = amount
                    s.yaw,s.pitch = -130.,40.
                    _,native = s.pose.native_pose()
                    expected_offset = s.perspective.offset(s.yaw,s.pitch,s.height)
                    s.update()
                    expected = tuple(a+(root+offset-a)*blend
                                     for a,root,offset in zip(native,(100,200,300),expected_offset))
                    for actual,wanted in zip(self.rendered_position(s),expected):
                        self.assertAlmostEqual(actual,wanted)

    def test_shoulder_additive_translation_restores_and_transition_returns_to_identity(self):
        original = (1e-7,-2e-7,3e-7)  # Inactive, within the constructor's identity tolerance.
        s,g = self.make_session(additive=original)
        s.perspective = Perspective(third=True)
        s.update()
        self.assertNotEqual(g.read(s.pose.additive_location,24),bytes(24))
        s.perspective = Perspective()
        s.update()
        for value in struct.unpack('<ddd',g.read(s.pose.additive_location,24)):
            self.assertEqual(value,0.)
        s.perspective = Perspective(third=True)
        s.update()
        with contextlib.redirect_stdout(io.StringIO()):s.restore()
        self.assertFalse(s.restore_failures)
        self.assertEqual(struct.unpack('<ddd',g.read(s.pose.additive_location,24)),original)

    def test_shoulder_swap_renders_smoothly_without_rewinding_anchor_or_touching_native_arm(self):
        s,g = self.make_session()
        s.yaw,s.pitch = 90.,0.
        s.perspective = Perspective(third=True,side=75,duration=.4)
        with patch('external_camera.time.monotonic',return_value=0):s.update()
        arm = g.read(s.arm,0x360)
        s.perspective.swap_shoulder(0)
        positions = []
        for now in (0.,.1,.2,.3,.4):
            self.propagate_root(s,-90.,(150.,220.,300.))
            g.writes.clear()
            with patch('external_camera.time.monotonic',return_value=now):s.update()
            self.assertNotIn(s.camera+0x200,[address for address,_ in g.writes])
            self.assertNotIn(s.pose.location,[address for address,_ in g.writes])
            self.assertEqual(g.read(s.arm,0x360),arm)
            pos = self.rendered_position(s)
            positions.append(pos)
            self.assertAlmostEqual(pos[1],0.)  # Distance stays 220 behind the player.
            self.assertAlmostEqual(pos[2],365.)
        self.assertEqual([round(p[0],5) for p in positions],[75.,98.4375,150.,201.5625,225.])

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
