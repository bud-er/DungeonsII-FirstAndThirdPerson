import contextlib
import io
import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from combat_profile import CombatProfile, aim_rotation, TARGET_WEAK_OFFSETS
from locomotion_facing import DodgeFacingIntent
from roll_direction import DODGE_BUTTONS


class Memory:
    def __init__(self):
        self.bytes = {}
        self.writes = []

    def read(self, address, size):
        return bytes(self.bytes.get(address+i, 0) for i in range(size))

    def write(self, address, data):
        self.writes.append((address, data))
        for i, value in enumerate(data):
            self.bytes[address+i] = value


class CombatProfileTests(unittest.TestCase):
    def profile(self):
        p = CombatProfile.__new__(CombatProfile)
        game = p.game = Memory()
        s = p.session = SimpleNamespace(game=game, pc=1000, pawn=2000, root=3000,
                                       yaw=135., pitch=12., identities={1000:1,2000:2,3000:3,4000:4,5000:5})
        s.identity = Mock(side_effect=lambda obj:s.identities[obj])
        s.pointer = Mock(side_effect=lambda obj, cls, name:
                         {('Pawn',1000):2000, ('CharacterMovement',2000):4000,
                          ('AutoTargeter',1000):5000}[(name,obj)])
        s.value = Mock(return_value=(0.,-75.,0.))
        p.movement, p.targeter = 4000, 5000
        p.control = {'address':1320}
        p.saved_control = struct.pack('<ddd',0.,42.,0.)
        p.records, p.restore_failures, p.applied = {},set(),False
        p.forward_targeting = Mock(restore_failures=set())
        p.ranged_facing = Mock(waiting=Mock(return_value=False))
        p.ranged_handoff_active = False
        p.movement_facing_enabled = False
        p.combat_activity = Mock(snapshot=Mock(return_value=(False,False)))
        p.axes,p.input_eligible,p.attack_held=(0.,0.),False,False
        p.dodge_facing=DodgeFacingIntent(DODGE_BUTTONS)
        p.facing_mode='camera'
        p.roll_direction=Mock()
        game.write(2010,b'\x50')
        game.write(4010,b'\x2e')
        game.write(5184,b'\x01')
        p.record(2000,2010,1,2,2)
        p.record(4000,4010,1,24,0)
        game.writes.clear()
        return p

    def test_camera_aim_includes_pitch_and_wraps_yaw(self):
        self.assertEqual(struct.unpack('<ddd',aim_rotation(405,-45)),(-45,45,0))

    def test_invalid_aim_is_rejected_before_any_write(self):
        p=self.profile()
        p.session.yaw=math.nan
        with self.assertRaises(RuntimeError):p.update()
        self.assertEqual(p.game.writes,[])

    def test_engine_orientation_flags_and_local_lock_suppression(self):
        p=self.profile();p.update()
        self.assertEqual(p.game.read(2010,1),b'\x52')
        self.assertEqual(p.game.read(4010,1),b'\x26')
        self.assertEqual(p.game.read(5184,1),b'\x01')
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12,135,0))
        expected={2010,4010,1320,*[5000+off for off in TARGET_WEAK_OFFSETS]}
        self.assertEqual({a for a,_ in p.game.writes},expected)

    def test_ranged_and_item_candidates_are_preserved(self):
        p=self.profile()
        p.game.write(5000+0x154,b'ranged!!')
        p.game.write(5000+0x18C,b'items!!!')
        p.game.writes.clear();p.update()
        self.assertEqual(p.game.read(5000+0x154,8),b'ranged!!')
        self.assertEqual(p.game.read(5000+0x18C,8),b'items!!!')
        self.assertEqual(p.game.read(5184,1),b'\x01')

    def test_native_ranged_turn_can_finish_without_moving_the_camera(self):
        p = self.profile()
        p.ranged_facing.waiting.return_value = True
        native_turn = struct.pack('<ddd', 0., -40., 0.)
        p.game.write(p.control['address'], native_turn)
        p.game.write(5000+0x154, b'ranged!!')
        p.game.writes.clear()
        p.set_input(0, (0., 0.))
        p.update()
        self.assertTrue(p.ranged_handoff_active)
        self.assertEqual(p.game.read(p.control['address'], 24), native_turn)
        self.assertNotIn(p.control['address'], {address for address, _ in p.game.writes})
        self.assertEqual((p.session.yaw, p.session.pitch), (135., 12.))
        self.assertEqual(p.game.read(5000+0x154, 8), b'ranged!!')
        self.assertEqual(p.game.read(2010, 1), b'\x52')

        # Native task completion returns camera-facing combat on the next tick.
        p.ranged_facing.waiting.return_value = False
        p.update()
        self.assertFalse(p.ranged_handoff_active)
        self.assertEqual(struct.unpack('<ddd', p.game.read(1320, 24)), (12., 135., 0.))

    def test_ranged_turn_handoff_does_not_depend_on_movement_input(self):
        for axes in ((0., 0.), (0., 1.), (0., -1.), (1., 0.)):
            with self.subTest(axes=axes):
                p = self.profile()
                p.ranged_facing.waiting.return_value = True
                p.set_input(0, axes)
                p.update()
                self.assertNotIn(1320, {address for address, _ in p.game.writes})

    def test_unreadable_native_turn_state_blocks_writes(self):
        p = self.profile()
        p.ranged_facing.waiting.side_effect = OSError('native input state unreadable')
        with self.assertRaises(OSError):
            p.update()
        self.assertFalse(p.game.writes)

    def test_rebuilding_input_tree_defers_combat_without_suspending_view(self):
        from native_ranged_facing import RangedStateRebuilt
        p = self.profile()
        p.ranged_facing.waiting.side_effect = RangedStateRebuilt('input tree changed')
        self.assertIs(p.update(), False)
        self.assertTrue(p.ranged_handoff_active)
        self.assertFalse(p.game.writes)

    def test_invalid_ranged_node_blocks_all_writes(self):
        p=self.profile();p.forward_targeting.validate.side_effect=RuntimeError('node changed')
        with self.assertRaises(RuntimeError):p.update()
        self.assertEqual(p.game.writes,[])

    def test_ranged_restore_failure_is_reported(self):
        p=self.profile();p.update();p.forward_targeting.restore_failures={12345}
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertIn(12345,p.restore_failures)
        self.assertTrue(p.applied)

    def test_default_combat_never_runs_legacy_force_writer(self):
        p=self.profile();p.set_input(0x80,(0,-1),now=100.);p.update()
        p.roll_direction.set_input.assert_not_called()
        p.roll_direction.validate.assert_not_called()
        p.roll_direction.update.assert_not_called()
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12,135,0))

    def test_unknown_ability_state_yields_rotation_without_disabling_camera(self):
        from native_combat_activity import ActivityUnavailable
        p=self.profile();p.movement_facing_enabled=True
        p.combat_activity.snapshot.side_effect=ActivityUnavailable('spec array rebuilt')
        p.set_input(0,(0,-1));p.update()
        self.assertEqual(p.facing_mode,'activity unavailable')
        self.assertNotIn(1320,{address for address,_ in p.game.writes})
        self.assertEqual((p.session.yaw,p.session.pitch),(135.,12.))

    def test_ability_scope_replacement_still_blocks_all_combat_writes(self):
        p=self.profile();p.movement_facing_enabled=True
        p.combat_activity.snapshot.side_effect=RuntimeError('local possession changed')
        with self.assertRaisesRegex(RuntimeError,'local possession changed'):p.update()
        self.assertFalse(p.game.writes)

    def test_backward_locomotion_faces_body_before_roll_without_editing_force(self):
        p=self.profile();p.movement_facing_enabled=True
        p.game.write(5000+0x154,b'ranged!!');p.game.writes.clear()
        p.set_input(0,(0.,-1.));p.update()
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,-45.,0.))
        self.assertEqual((p.session.yaw,p.session.pitch),(135.,12.))
        self.assertEqual(p.facing_mode,'movement')
        self.assertEqual(p.game.read(5000+0x154,8),b'ranged!!')
        p.roll_direction.update.assert_not_called()
        p.combat_activity.snapshot.return_value=(False,True)
        p.game.writes.clear();p.set_input(0x80,(0.,-1.));p.update()
        self.assertNotIn(1320,{address for address,_ in p.game.writes})
        self.assertEqual(p.facing_mode,'native roll')
        # Releasing movement and turning the camera cannot steer a committed roll.
        p.session.yaw=20.;p.set_input(0,(0.,0.),attack_held=True)
        p.game.writes.clear();p.update()
        self.assertNotIn(1320,{address for address,_ in p.game.writes})

    def test_physical_and_native_attack_both_take_priority_over_backpedaling(self):
        for held,active in ((True,False),(False,True)):
            with self.subTest(held=held,active=active):
                p=self.profile();p.movement_facing_enabled=True
                p.combat_activity.snapshot.return_value=(active,False)
                p.set_input(0,(0.,-1.),attack_held=held);p.update()
                self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,135.,0.))
                self.assertEqual(p.facing_mode,'attack')

    def pending_dodge(self, p, button=0x10000):
        p.movement_facing_enabled=True
        p.session.perspective=SimpleNamespace(amount=1.,target=1.)
        p.set_input(0,(0.,-1.),attack_held=True,now=10.)
        p.set_input(button,(0.,-1.),attack_held=True,now=10.01)

    def test_fresh_third_person_dodge_precedes_physical_and_native_attack_facing(self):
        for button in (0x10000,0x80):
            for native in (False,True):
                with self.subTest(button=button,native=native):
                    p=self.profile();self.pending_dodge(p,button)
                    p.combat_activity.snapshot.return_value=(native,False)
                    p.attack_held=not native
                    p.update(now=10.02)
                    self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,-45.,0.))
                    self.assertEqual(p.facing_mode,'dodge requested')
                    self.assertEqual((p.session.yaw,p.session.pitch),(135.,12.))
                    self.assertEqual({a for a,_ in p.game.writes},
                        {2010,4010,1320,*[5000+off for off in TARGET_WEAK_OFFSETS]})
                    p.roll_direction.update.assert_not_called()

    def test_rejected_dodge_request_expires_while_attack_and_dodge_remain_held(self):
        p=self.profile();self.pending_dodge(p)
        p.update(now=10.02)
        p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.17)
        p.update(now=10.17)
        self.assertEqual(p.facing_mode,'attack')
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,135.,0.))

    def test_native_roll_cancels_pending_without_writing_or_replaying_it_after_roll(self):
        p=self.profile();self.pending_dodge(p)
        p.combat_activity.snapshot.return_value=(True,True)
        p.update(now=10.02)
        self.assertNotIn(1320,{a for a,_ in p.game.writes})
        self.assertEqual(p.facing_mode,'native roll')
        p.combat_activity.snapshot.return_value=(True,False)
        p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.03)
        p.update(now=10.03)
        self.assertEqual(p.facing_mode,'attack')

    def test_native_locks_cancel_dodge_request_without_changing_lock_or_delaying_request(self):
        p=self.profile();self.pending_dodge(p)
        p.game.write(p.session.pc+0x339,b'\x01');p.game.writes.clear()
        p.update(now=10.02)
        self.assertEqual(p.facing_mode,'attack')
        self.assertEqual(p.game.read(p.session.pc+0x339,1),b'\x01')
        self.assertNotIn(p.session.pc+0x339,{a for a,_ in p.game.writes})
        p.game.write(p.session.pc+0x339,b'\0')
        p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.03)
        p.update(now=10.03)
        self.assertEqual(p.facing_mode,'attack')

    def test_ranged_handoff_and_unknown_activity_cancel_pending_without_replay(self):
        from native_combat_activity import ActivityUnavailable
        from native_ranged_facing import RangedStateRebuilt
        for condition in ('ranged','activity','rebuild'):
            with self.subTest(condition=condition):
                p=self.profile();self.pending_dodge(p)
                if condition=='ranged':p.ranged_facing.waiting.return_value=True
                elif condition=='activity':p.combat_activity.snapshot.side_effect=ActivityUnavailable('rebuild')
                else:p.ranged_facing.waiting.side_effect=RangedStateRebuilt('rebuild')
                p.update(now=10.02)
                self.assertNotIn(1320,{a for a,_ in p.game.writes})
                p.ranged_facing.waiting.return_value=False
                p.ranged_facing.waiting.side_effect=None
                p.combat_activity.snapshot.side_effect=None
                p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.03)
                p.update(now=10.03)
                self.assertEqual(p.facing_mode,'attack')

    def test_first_person_and_transition_keep_existing_attack_priority(self):
        for amount,target in ((0.,0.),(.98,1.),(1.,0.)):
            with self.subTest(amount=amount,target=target):
                p=self.profile();p.movement_facing_enabled=True
                p.session.perspective=SimpleNamespace(amount=amount,target=target)
                p.set_input(0,(0.,-1.),attack_held=True,now=10.)
                p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.01)
                p.update(now=10.02)
                self.assertEqual(p.facing_mode,'attack')
                self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,135.,0.))

    def test_leaving_third_person_cancels_pending_before_combat_write(self):
        p=self.profile();self.pending_dodge(p)
        p.session.perspective.target=0.
        p.update(now=10.02)
        self.assertEqual(p.facing_mode,'attack')
        p.session.perspective.target=1.
        p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.03)
        p.update(now=10.03)
        self.assertEqual(p.facing_mode,'attack')

    def test_focus_or_menu_restore_requires_new_release_before_dodge_request(self):
        for restore in (False,True):
            p=self.profile();self.pending_dodge(p)
            if restore:
                with contextlib.redirect_stdout(io.StringIO()):p.restore()
            else:p.set_input(0,(0.,0.),eligible=False,now=10.02)
            p.set_input(0x10000,(0.,-1.),attack_held=True,now=10.03)
            p.update(now=10.03)
            self.assertEqual(p.facing_mode,'attack')

    def test_native_ranged_facing_precedes_movement_and_attack_priority(self):
        p=self.profile();p.movement_facing_enabled=True
        p.ranged_facing.waiting.return_value=True
        p.set_input(0,(0.,-1.),attack_held=True);p.update()
        self.assertNotIn(1320,{address for address,_ in p.game.writes})
        self.assertEqual(p.facing_mode,'native ranged turn')

    def test_movement_lock_or_focus_loss_prevents_locomotion_facing(self):
        for eligible,locked in ((False,False),(True,True)):
            p=self.profile();p.movement_facing_enabled=True
            p.game.write(p.session.pc+0x339,bytes([locked]));p.game.writes.clear()
            p.set_input(0,(0.,-1.),eligible=eligible);p.update()
            self.assertNotIn(1320,{address for address,_ in p.game.writes})

    def test_neutral_after_roll_returns_body_to_view(self):
        p=self.profile();p.movement_facing_enabled=True
        p.set_input(0,(0.,0.));p.update()
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12.,135.,0.))
        self.assertEqual(p.facing_mode,'camera')

    def test_native_roll_comparison_keeps_aiming_and_restores_cleanly(self):
        p=self.profile();p.roll_direction=None
        p.set_input(0x80,(0,-1),now=100.);p.update()
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(12,135,0))
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertFalse(p.restore_failures)

    def test_native_bow_comparison_leaves_ranged_rows_and_cache_alone(self):
        p=self.profile();p.forward_targeting=None
        p.game.write(5000+0x154,b'ranged!!');p.game.writes.clear();p.update()
        self.assertEqual(p.game.read(5000+0x154,8),b'ranged!!')
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertFalse(p.restore_failures)

    def test_restore_preserves_unrelated_game_bits_and_current_facing(self):
        p=self.profile();p.update()
        p.game.write(2010,b'\xd2')
        p.game.write(4010,b'\xa6')
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertEqual(p.game.read(2010,1),b'\xd0')
        self.assertEqual(p.game.read(4010,1),b'\xae')
        self.assertEqual(p.game.read(5184,1),b'\x01')
        self.assertEqual(struct.unpack('<ddd',p.game.read(1320,24)),(0,-75,0))
        self.assertFalse(p.restore_failures)

    def test_never_restore_stale_target_candidates(self):
        p=self.profile();p.update();p.game.writes.clear()
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertTrue(all(a not in [5000+off for off in TARGET_WEAK_OFFSETS] for a,_ in p.game.writes))

    def test_recycled_player_blocks_all_update_writes(self):
        p=self.profile();p.session.identity.return_value=7;p.session.identity.side_effect=None
        with self.assertRaises(RuntimeError):p.update()
        self.assertEqual(p.game.writes,[])

    def test_recycled_targeter_is_not_restored_or_handed_off(self):
        p=self.profile();p.update();p.game.writes.clear()
        p.session.identity.side_effect=lambda obj:99 if obj==5000 else p.session.identities[obj]
        with contextlib.redirect_stdout(io.StringIO()):p.restore()
        self.assertTrue(p.restore_failures)
        self.assertTrue(all(a not in (5184,1320) for a,_ in p.game.writes))

    def test_component_replacement_blocks_update(self):
        p=self.profile();p.session.pointer.return_value=999;p.session.pointer.side_effect=None
        with self.assertRaises(RuntimeError):p.update()
        self.assertEqual(p.game.writes,[])


if __name__=='__main__':unittest.main()
