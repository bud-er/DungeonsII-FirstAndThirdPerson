from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import contextlib
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from local_view_target import LocalViewTarget
from camera_pose import CameraPose, rotator_quaternion
from test_keyboard_movement import Memory


class DialogueView(unittest.TestCase):
    def heading_profile(self):
        p,g,ids=self.profile()
        p.cache_rotation,p.cache_written=6000,None
        g.put(1000,struct.pack('<Q',200));g.put(2000,bytes(8))
        g.put(6000,struct.pack('<3d',0.,0.,0.))
        g.put(p.session.camera+0x1E0,struct.pack('<4d',*rotator_quaternion(-15,70)))
        return p,g

    def test_heading_trial_only_updates_current_local_view_angle(self):
        p,g=self.heading_profile()
        p.sync_heading(True)
        self.assertEqual([a for a,_ in g.writes],[6000])
        actual=struct.unpack('<3d',g.read(6000,24))
        for a,b in zip(actual,(-15.,70.,0.)):self.assertAlmostEqual(a,b)

    def test_heading_trial_leaves_other_targets_and_disabled_modes_alone(self):
        p,g=self.heading_profile()
        p.sync_heading(False);self.assertFalse(g.writes)
        g.put(1000,struct.pack('<Q',500))
        p.sync_heading(True);self.assertFalse(g.writes)
        g.put(1000,struct.pack('<Q',200));g.put(2000,struct.pack('<Q',500))
        p.sync_heading(True);self.assertFalse(g.writes)

    def test_heading_trial_restores_current_native_angle_without_rewinding_position(self):
        p,g=self.heading_profile();p.sync_heading(True);g.writes.clear()
        p.session.pose.native_pose=Mock(return_value=(rotator_quaternion(-40,135),(99,88,77)))
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual([a for a,_ in g.writes],[6000])
        for a,b in zip(struct.unpack('<3d',g.read(6000,24)),(-40,135,0)):self.assertAlmostEqual(a,b)

    def test_heading_trial_preserves_native_cache_replacement(self):
        p,g=self.heading_profile();p.sync_heading(True)
        replacement=struct.pack('<3d',1,2,3);g.put(6000,replacement);g.writes.clear()
        p.restore();self.assertFalse(g.writes);self.assertEqual(g.read(6000,24),replacement)

    def profile(self):
        p = LocalViewTarget.__new__(LocalViewTarget)
        g = p.game = Memory()
        ids = {100:'pc', 200:'pawn', 0x4000:'cam', 400:'manager', 500:'npc', 600:'npc2'}
        s = p.session = SimpleNamespace(pc=100, pawn=200, camera=0x4000, fov=115, game=g,
                                       identity=lambda obj: ids[obj])
        s.pointer = lambda obj, cls, name: 400 if name == 'PlayerCameraManager' else 100
        s.field = lambda *args: {'address':7000, 'bool':{'byteMask':1}}
        s.reflection = Mock()
        s.reflection.header.side_effect = lambda obj: {'class':800, 'name':'BP_MerchantVillager_C'}
        s.is_a = lambda cls, name: name == 'QuestCharacter'
        p.manager, p.identity, p.slots = 400, 'manager', [1000,2000]
        p.state_offset = 0x8d0
        p.records, p.restore_failures, p.seen_dialogue = {}, set(), False
        g.put(1000,struct.pack('<Q',500));g.put(2000,struct.pack('<Q',600))
        g.put(s.camera+0x1e0,struct.pack('<12d',0,0,0,1,10,20,55,0,1,1,1,0))
        s.pose = CameraPose.__new__(CameraPose)
        s.pose.session,s.pose.game = s,g
        s.pose.additive_location = s.camera+0x2B0
        s.pose.native_fov_address,s.pose.additive_fov = s.camera+0x240,s.camera+0x2F4
        g.put(s.pose.native_fov_address,struct.pack('<f',115.))
        return p,g,ids

    def test_dialogue_selects_existing_player_component_without_editing_npcs(self):
        p,g,ids = self.profile()
        with contextlib.redirect_stdout(io.StringIO()):
            p.update(True)
        self.assertEqual((g.u64(1000),g.u64(2000)),(200,200))
        pose=struct.unpack('<ddddddff',g.read(1016,56))
        self.assertEqual(pose,(10,20,55,0,0,0,115,115))
        self.assertEqual({addr for addr,_ in g.writes},{1000,1016,2000,2016})
        count=len(g.writes)
        p.update(True)
        self.assertEqual(len(g.writes),count)  # No repeated world/cache position writes.

    def test_f6_off_during_dialogue_restores_native_targets(self):
        p,g,ids=self.profile()
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual((g.u64(1000),g.u64(2000)),(500,600))

    def test_shoulder_dialogue_targets_use_rendered_offset_and_actual_fov(self):
        p,g,ids = self.profile()
        s = p.session
        g.put(s.camera+0x1e0,struct.pack('<dddd',*rotator_quaternion(0,90)))
        g.put(s.pose.additive_location,struct.pack('<ddd',-220.,45.,10.))
        g.put(s.pose.native_fov_address,struct.pack('<f',45.))
        g.put(s.pose.additive_fov,struct.pack('<f',45.))
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        for address in p.slots:
            actual = struct.unpack('<ddddddff',g.read(address+0x10,56))
            for a,b in zip(actual,(-35.,-200.,65.,0.,90.,0.,90.,90.)):
                self.assertAlmostEqual(a,b)
        # Closing the shop releases stale NPC records without changing the
        # final shoulder pose, rebuilding the session, or touching the camera.
        before = list(g.writes)
        p.update(False)
        self.assertFalse(p.records)
        self.assertEqual(g.writes,before)

    def test_invalid_final_camera_pose_is_not_published_to_view_targets(self):
        for address,value in (('additive_location',struct.pack('<ddd',float('nan'),0.,0.)),
                              ('additive_fov',struct.pack('<f',float('nan')))):
            p,g,ids = self.profile()
            g.put(getattr(p.session.pose,address),value)
            with self.assertRaises(RuntimeError):p.update(True)
            self.assertFalse(g.writes)
            self.assertEqual((g.u64(1000),g.u64(2000)),(500,600))

    def test_native_player_state_tracking_is_restored_with_the_npc_target(self):
        p,g,ids=self.profile()
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        g.put(1000+p.state_offset,struct.pack('<Q',100))
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual(g.u64(1000+p.state_offset),0)
        self.assertEqual(g.u64(1000),500)

    def test_dialogue_close_does_not_resurrect_old_npc_view(self):
        p,g,ids=self.profile()
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        g.put(2000,bytes(8))  # Native blend completed; pending target cleared.
        p.update(False)
        self.assertFalse(p.records)
        p.restore()
        self.assertEqual((g.u64(1000),g.u64(2000)),(200,0))

    def test_true_other_camera_is_refused_and_not_overwritten(self):
        p,g,ids=self.profile()
        p.session.reflection.header.side_effect=lambda obj:{'class':800,'name':'CinematicCameraActor'}
        self.assertEqual(p.reason(),'game view target')
        with self.assertRaises(RuntimeError):p.update(True)
        self.assertFalse(g.writes)

    def test_remote_player_is_never_a_dialogue_target(self):
        p,g,ids=self.profile()
        p.session.is_a=lambda cls,name:name in ('QuestCharacter','PlayerCharacter')
        self.assertEqual(p.reason(),'game view target')
        with self.assertRaises(RuntimeError):p.update(True)
        self.assertFalse(g.writes)

    def test_recycled_npc_is_not_restored(self):
        p,g,ids=self.profile()
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        ids[500]='new npc'
        p.restore()
        self.assertEqual(g.u64(1000),200)
        self.assertFalse(p.restore_failures)

    def test_native_target_change_survives_restoration(self):
        p,g,ids=self.profile()
        with contextlib.redirect_stdout(io.StringIO()):p.update(True)
        g.put(1000,struct.pack('<Q',999))
        p.restore()
        self.assertEqual(g.u64(1000),999)

    def test_wrong_owner_or_replicated_manager_is_never_written(self):
        for replicated in (False,True):
            p,g,ids=self.profile()
            if replicated:g.put(7000,b'\x01')
            else:p.session.pointer=lambda *args:999
            with self.assertRaises(RuntimeError):p.update(True)
            self.assertFalse(g.writes)

