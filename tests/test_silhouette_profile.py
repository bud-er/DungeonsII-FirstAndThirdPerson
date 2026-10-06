from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from silhouette_profile import SilhouetteProfile,SilhouetteRecord
from external_camera import CameraSession


class Memory:
    def __init__(self):
        self.data={110:b'\x01',210:b'\x01',310:b'\x01'}
        self.writes=[]
        self.fail=False
    def read(self,address,size):return self.data[address]
    def write(self,address,data):
        if self.fail:raise OSError('write failed')
        self.writes.append((address,data));self.data[address]=data


class Silhouettes(unittest.TestCase):
    def make(self,flags=0x201):
        depth_patch=patch('mob_depth_profile.MobDepthProfile')
        depth=depth_patch.start().return_value
        depth.restore_failures=set()
        depth.restore_failure_details={}
        self.addCleanup(depth_patch.stop)
        g=Memory()
        prop=dict(name='EnableOcclusionSilhouetting',type='BoolProperty',size=1,flags=flags,
                  bool=dict(fieldSize=1,byteOffset=0,byteMask=1))
        ids={100:1,200:2,300:3}
        s=SimpleNamespace(game=g,layouts={'BaseCharacter':{'properties':[prop]}},
                          ensure_layout=lambda _:None,identity=lambda obj:ids[obj],
                          field=lambda obj,cls,name:dict(prop,address=obj+10))
        s.object_retired=lambda obj,expected:ids.get(obj)!=expected
        classes={100:'mob',200:'mob',300:'player'}
        s.reflection=SimpleNamespace(headers={},header=lambda obj:{'class':classes[obj]})
        s.is_a=lambda cls,name:name in ({'BaseCharacter','MobCharacter'} if cls=='mob'
                                       else {'BaseCharacter','PlayerCharacter'})
        scene=SimpleNamespace(characters={100:1})
        return SilhouetteProfile(s,scene),g,ids

    def test_hides_then_restores_only_the_silhouette_bit(self):
        p,g,_=self.make();g.data[110]=b'\x81'
        p.update();self.assertEqual(g.data[110],b'\x80')
        g.data[110]=b'\x40'  # Unrelated game-owned bits change while enabled.
        p.restore();self.assertEqual(g.data[110],b'\x41')
        self.assertFalse(p.restore_failures)

    def test_already_disabled_silhouette_is_never_enabled(self):
        p,g,_=self.make();g.data[110]=b'\x00'
        p.update();p.restore()
        self.assertFalse(g.writes)

    def test_replicated_or_unverified_field_is_refused(self):
        with self.assertRaisesRegex(RuntimeError,'replicated'):self.make(flags=0x20)

    def test_players_are_excluded_even_if_catalogued_by_mistake(self):
        p,g,_=self.make();p.scene.characters[300]=3
        p.update()
        self.assertNotIn(300,p.records)
        self.assertEqual(g.data[310],b'\x01')

    def test_newly_discovered_mob_is_hidden(self):
        p,g,_=self.make();p.update()
        p.scene.characters[200]=2;p.next_update=0
        p.update();self.assertEqual(g.data[210],b'\x00')
        p.restore();self.assertEqual(g.data[210],b'\x01')

    def test_recycled_object_is_not_written_on_restore(self):
        p,g,ids=self.make();p.update();g.writes.clear()
        ids[100]=9
        p.restore();self.assertFalse(g.writes)
        self.assertFalse(p.restore_failures)

    def test_removed_mob_does_not_disable_camera(self):
        p,g,ids=self.make();p.update();g.data[110]=b'\x01';del ids[100]
        p.session.identity=Mock(side_effect=OSError(299,'freed actor header'))
        self.assertFalse(p.records[100].update())
        p.session.identity.assert_not_called()

    def test_freed_actor_slot_is_checked_before_even_disabled_flag_reads(self):
        p,g,ids=self.make();p.update()
        self.assertEqual(g.data[110],b'\x00')
        del ids[100]
        p.session.identity=Mock(side_effect=OSError(299,'freed actor header'))
        g.read=Mock(side_effect=OSError(299,'freed actor field'))
        g.writes.clear();record=p.records[100]
        self.assertFalse(record.update())
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertFalse(record.applied)
        p.session.identity.assert_not_called();g.read.assert_not_called()
        self.assertFalse(g.writes)

    def test_reused_actor_pointer_is_not_read_or_written(self):
        p,g,ids=self.make();p.update();ids[100]=99
        p.session.identity=Mock(side_effect=AssertionError('replacement header read'))
        g.read=Mock(side_effect=AssertionError('replacement field read'))
        g.writes.clear();record=p.records[100]
        self.assertFalse(record.update());self.assertTrue(record.restore())
        p.session.identity.assert_not_called();g.read.assert_not_called()
        self.assertFalse(g.writes)

    def test_freed_actor_restore_does_not_leave_a_recovery_blocker(self):
        p,g,ids=self.make();p.update();del ids[100]
        p.session.identity=Mock(side_effect=OSError(299,'freed actor header'))
        g.read=Mock(side_effect=OSError(299,'freed actor field'));g.writes.clear()
        for _ in range(3):
            p.restore()
            self.assertFalse(p.restore_failures)
        p.session.identity.assert_not_called();g.read.assert_not_called()
        self.assertFalse(g.writes)

    def test_unreadable_live_slot_is_not_retired_and_retry_restores(self):
        p,g,_=self.make();p.update();check=p.session.object_retired
        p.session.object_retired=Mock(side_effect=OSError(299,'slot unavailable'))
        g.writes.clear();p.restore()
        self.assertEqual(p.restore_failures,{100})
        self.assertTrue(p.records[100].applied)
        self.assertIn('actor 0x64 restore/object-slot retirement check',p.restore_failure_details[100])
        self.assertFalse(g.writes)
        p.session.object_retired=check;p.restore()
        self.assertFalse(p.restore_failures);self.assertFalse(p.restore_failure_details)
        self.assertEqual(g.data[110],b'\x01')

    def test_live_header_runtime_error_is_not_assumed_retirement(self):
        p,g,_=self.make();p.update();identity=p.session.identity
        p.session.identity=Mock(side_effect=RuntimeError('header unstable'))
        g.writes.clear();p.restore()
        self.assertEqual(p.restore_failures,{100})
        self.assertTrue(p.records[100].applied)
        self.assertIn('actor identity check',p.restore_failure_details[100])
        p.session.identity=identity;p.restore();self.assertFalse(p.restore_failures)
        self.assertEqual(g.data[110],b'\x01')

    def test_live_actor_missing_from_catalogue_restores_before_forgetting(self):
        p,g,_=self.make();p.update();record=p.records[100]
        p.scene.characters.clear();p.next_update=0;p.update()
        self.assertNotIn(100,p.records)
        self.assertFalse(record.applied)
        self.assertEqual(g.data[110],b'\x01')

    def test_catalogue_drop_cannot_discard_a_failed_live_restore(self):
        p,g,_=self.make();p.update();record=p.records[100]
        p.scene.characters.clear();p.next_update=0;g.fail=True
        with self.assertRaisesRegex(RuntimeError,'actor 0x64 update/silhouette flag write'):
            p.update()
        self.assertIs(p.records[100],record);self.assertTrue(record.applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures)
        self.assertEqual(g.data[110],b'\x01')

    def test_catalogue_identity_change_restores_before_recapture(self):
        p,g,_=self.make();p.update();record=p.records[100]
        p.scene.characters[100]=7
        p.collect()  # The slot still holds the old live actor: restore it.
        self.assertIs(p.records[100],record)  # New identity isn't valid yet.
        self.assertFalse(record.applied)
        self.assertEqual(g.data[110],b'\x01')

    def test_catalogue_replacement_cannot_erase_a_failed_original_restore(self):
        p,g,_=self.make();p.update();record=p.records[100]
        p.scene.characters[100]=7;g.fail=True
        with self.assertRaisesRegex(RuntimeError,'actor 0x64 replacement restore'):
            p.collect()
        self.assertIs(p.records[100],record);self.assertTrue(record.applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures)

    def test_live_field_read_error_identifies_actor_and_stage(self):
        p,g,_=self.make();p.update();p.next_update=0
        g.read=Mock(side_effect=OSError(299,'live field unreadable'))
        with self.assertRaisesRegex(RuntimeError,'actor 0x64 update/silhouette flag read'):
            p.update()
        self.assertTrue(p.records[100].applied)

    def test_depth_failure_details_are_preserved_alongside_actor_details(self):
        p,_,_=self.make()
        p.depth.restore_failures={400}
        p.depth.restore_failure_details={400:'Mob depth mesh 0x190 restore/read: unavailable'}
        p.restore()
        self.assertEqual(p.restore_failures,{400})
        self.assertEqual(p.restore_failure_details,p.depth.restore_failure_details)

    def test_partial_write_and_restore_failure_remain_retryable(self):
        p,g,_=self.make();p.collect();g.fail=True
        with self.assertRaises(OSError):p.records[100].update()
        p.restore();self.assertEqual(p.restore_failures,{100})
        g.fail=False;p.restore()
        self.assertFalse(p.restore_failures)
        self.assertFalse(p.records[100].applied)

    def test_toggle_restores_immediately_and_recaptures_changed_native_flags(self):
        p,g,_=self.make()
        s=CameraSession.__new__(CameraSession)
        s.__dict__.update(vars(p.session))
        s.silhouette_profile=p
        p.update()
        s.set_silhouette_hiding(p.scene,False)
        self.assertEqual(g.data[110],b'\x01')
        self.assertIsNone(s.silhouette_profile)
        g.data[110]=b'\x00'
        s.set_silhouette_hiding(p.scene,True)
        s.silhouette_profile.update()
        s.set_silhouette_hiding(p.scene,False)
        self.assertEqual(g.data[110],b'\x00')

    def test_failed_toggle_retains_restoration_records(self):
        p,g,_=self.make()
        s=CameraSession.__new__(CameraSession)
        s.silhouette_profile=p
        p.update();g.fail=True
        with self.assertRaisesRegex(RuntimeError,'restoration failed'):
            s.set_silhouette_hiding(p.scene,False)
        self.assertIs(s.silhouette_profile,p)
        g.fail=False
        s.set_silhouette_hiding(p.scene,False)
        self.assertEqual(g.data[110],b'\x01')


if __name__=='__main__':unittest.main()
