from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mob_depth_profile import MobDepthProfile, RENDER_DIRTY_GUARD
from test_keyboard_movement import Memory


class MobDepthTests(unittest.TestCase):
    def make(self):
        g=Memory();g.base=0x100000
        rva,guard=RENDER_DIRTY_GUARD;g.put(g.base+rva,guard)
        ids={1000:1,2000:2,3000:3,4000:4,5000:5,6000:6,7000:7}
        classes={1000:'mob',2000:'scene',3000:'mesh',4000:'mesh',5000:'widget',6000:'player',7000:'mesh'}
        outer={1000:0,2000:1000,3000:1000,4000:1000,5000:1000,6000:0,7000:6000}
        parents={3000:2000,4000:3000,5000:3000,7000:2000}
        children={2000:[3000,7000],3000:[4000,5000],4000:[],5000:[],7000:[]}
        prop=dict(name='bRenderCustomDepth',type='BoolProperty',offset=0x271,size=1,flags=0x201,
                  bool=dict(byteMask=0x40,fieldSize=1,byteOffset=0))
        s=SimpleNamespace(game=g,identity=lambda o:ids[o],layouts={'PrimitiveComponent':{'properties':[prop]}},ensure_layout=lambda _:None)
        s.object_retired=lambda obj,expected:ids.get(obj)!=expected
        s.reflection=SimpleNamespace(headers={},header=lambda o:dict(class_=classes[o],**{'class':classes[o]},outer=outer[o],flags=0))
        chains={'mob':{'Actor','MobCharacter'},'player':{'Actor','MobCharacter','PlayerCharacter'},
                'mesh':{'SceneComponent','PrimitiveComponent','MeshComponent'},'scene':{'SceneComponent'},'widget':{'SceneComponent'}}
        s.is_a=lambda cls,name:name in chains[cls]
        s.pointer=lambda obj,cls,name:2000 if (obj,name)==(1000,'RootComponent') else parents[obj]
        s.field=lambda obj,cls,name:dict(prop,address=obj+0x271)
        def value(obj,cls,name,fmt):
            data=b''.join(struct.pack('<Q',p) for p in children.get(obj,[]))
            g.put(obj+10000,data)
            return obj+10000,len(data)//8,len(data)//8
        s.value=value
        for obj in (3000,4000,5000,7000):
            g.put(obj+0x94,b'\x03\x20');g.put(obj+0x271,b'\xc0')
        scene=SimpleNamespace(characters={1000:1,6000:6})
        return MobDepthProfile(s,scene),g,ids,parents,children

    def test_only_mob_mesh_and_owned_mesh_children_suppressed(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        self.assertEqual(set(p.records),{3000,4000})
        self.assertEqual(g.read(3000+0x271,1),b'\x80')
        self.assertEqual(g.read(4000+0x271,1),b'\x80')
        self.assertEqual(g.read(5000+0x271,1),b'\xc0')  # UI untouched.
        self.assertEqual(g.read(7000+0x271,1),b'\xc0')  # Attached player untouched.
        self.assertTrue(all(len(v)==1 for _,v in g.writes))

    def test_render_cache_invalidated_and_unrelated_flags_preserved_on_restore(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        self.assertEqual(g.read(3000+0x95,1),b'\x21')
        g.put(3000+0x271,b'\x08');p.restore()
        self.assertEqual(g.read(3000+0x271,1),b'\x48')
        self.assertFalse(p.restore_failures)

    def test_native_highlight_reenable_is_suppressed_without_repeated_dirty_writes(self):
        p,g,_,_,_=self.make();p.update(budget=1.);g.writes.clear();p.update(budget=1.)
        self.assertFalse(g.writes)
        g.put(3000+0x271,b'\x40');p.update(budget=1.)
        self.assertEqual(g.read(3000+0x271,1),b'\0')

    def test_already_disabled_pass_is_never_enabled_by_restore(self):
        p,g,_,_,_=self.make();g.put(3000+0x271,b'\0');p.update(budget=1.);p.restore()
        self.assertEqual(g.read(3000+0x271,1),b'\0')

    def test_new_attached_mesh_discovered(self):
        p,g,_,_,children=self.make();children[3000]=[5000];p.update(budget=1.)
        self.assertNotIn(4000,p.records)
        children[3000].append(4000);p.next_collect=0;p.update(budget=1.)
        self.assertEqual(g.read(4000+0x271,1),b'\x80')

    def test_recycled_component_serial_never_written(self):
        p,g,ids,_,_=self.make();p.update(budget=1.);ids[3000]=99;g.writes.clear();p.restore()
        self.assertFalse(any(a in (3000+0x95,3000+0x271) for a,_ in g.writes))

    def test_freed_component_retired_slot_is_checked_before_old_memory(self):
        p,g,ids,_,_=self.make();p.update(budget=1.)
        del ids[3000]
        original_identity=p.session.identity
        def identity(obj):
            if obj==3000:raise OSError(299,'freed component memory')
            return original_identity(obj)
        p.session.identity=Mock(side_effect=identity)
        original_read=g.read
        def read(address,size):
            if 3000<=address<3000+0x280:raise OSError(299,'freed component memory')
            return original_read(address,size)
        g.read=read;g.writes.clear()
        record=p.records[3000]
        self.assertFalse(record.update())
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertFalse(record.applied)
        self.assertFalse(any(call.args==(3000,) for call in p.session.identity.call_args_list))
        self.assertFalse(any(3000<=address<3000+0x280 for address,_ in g.writes))

    def test_unreadable_live_slot_remains_a_blocker_and_is_retryable(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        check=p.session.object_retired
        def retired(obj,expected):
            if obj==3000:raise OSError(299,'object slot unreadable')
            return check(obj,expected)
        p.session.object_retired=retired;g.writes.clear();p.restore()
        self.assertEqual(p.restore_failures,{3000})
        self.assertTrue(p.records[3000].applied)
        self.assertIn('mesh 0xBB8 (actor 0x3E8) restore/object-slot retirement check',
                      p.restore_failure_details[3000])
        self.assertFalse(any(a in (3000+0x95,3000+0x271) for a,_ in g.writes))
        p.session.object_retired=check;p.restore()
        self.assertFalse(p.restore_failures)
        self.assertFalse(p.restore_failure_details)
        self.assertEqual(g.read(3000+0x271,1),b'\xc0')

    def test_unreadable_live_header_runtime_error_is_not_retirement(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        identity=p.session.identity
        def unavailable(obj):
            if obj==3000:raise RuntimeError('header not stable')
            return identity(obj)
        p.session.identity=unavailable;g.writes.clear();p.restore()
        self.assertEqual(p.restore_failures,{3000})
        self.assertTrue(p.records[3000].applied)
        p.session.identity=identity;p.restore()
        self.assertFalse(p.restore_failures)

    def test_reused_pointer_does_not_read_or_write_replacement(self):
        p,g,ids,_,_=self.make();p.update(budget=1.)
        record=p.records[3000];ids[3000]=999
        p.session.identity=Mock(side_effect=AssertionError('replacement header read'))
        g.writes.clear()
        self.assertTrue(record.restore())
        self.assertFalse(record.applied)
        p.session.identity.assert_not_called()
        self.assertFalse(g.writes)

    def test_detached_live_component_restores_before_retiring(self):
        p,g,_,parents,_=self.make();p.update(budget=1.);parents[4000]=9000;p.update(budget=1.)
        self.assertEqual(g.read(4000+0x271,1),b'\xc0')
        self.assertNotIn(4000,p.records)

    def test_live_unregistered_component_restores_preference_without_dirtying(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        g.put(3000+0x94,b'\0');g.writes.clear()
        self.assertTrue(p.records[3000].restore())
        self.assertEqual(g.read(3000+0x271,1),b'\xc0')
        self.assertFalse(any(a==3000+0x95 for a,_ in g.writes))

    def test_changed_attachment_is_restored_before_replacement_baseline(self):
        p,g,_,parents,children=self.make();p.update(budget=1.)
        old=p.records[4000]
        parents[4000]=2000;children[2000].append(4000);children[3000].remove(4000)
        p.collect()
        self.assertIsNot(p.records[4000],old)
        self.assertFalse(old.applied)
        self.assertEqual(p.records[4000].original,0x40)
        self.assertEqual(g.read(4000+0x271,1),b'\xc0')

    def test_changed_attachment_failed_restore_cannot_replace_applied_record(self):
        p,g,_,parents,children=self.make();p.update(budget=1.)
        old=p.records[4000]
        parents[4000]=2000;children[2000].append(4000);children[3000].remove(4000)
        g.fail=True;p.collect()
        self.assertIs(p.records[4000],old)
        self.assertTrue(old.applied)
        p.restore();self.assertIn(4000,p.restore_failures)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures)

    def test_interleaved_collection_does_not_update_replaced_record(self):
        p,_,_,_,_=self.make();p.update(budget=1.)
        iterator=p.update_steps();next(iterator)
        old=p.records[4000];old.update=Mock(side_effect=AssertionError('stale iterator record'))
        p.records[4000]=Mock()
        list(iterator)
        old.update.assert_not_called()

    def test_unregistered_mesh_not_suppressed(self):
        p,g,_,_,_=self.make();g.put(3000+0x94,b'\0');p.update(budget=1.)
        self.assertEqual(g.read(3000+0x271,1),b'\xc0')
        self.assertNotIn(3000,p.records)

    def test_partial_write_restoration_can_be_retried(self):
        p,g,_,_,_=self.make();p.collect();g.fail=True
        with self.assertRaises(OSError):p.records[3000].update()
        self.assertTrue(p.records[3000].applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures)

    def test_failed_restore_retains_record_and_reports_failure(self):
        p,g,_,_,_=self.make();p.update(budget=1.);g.fail=True;p.restore()
        self.assertEqual(p.restore_failures,{3000,4000})
        self.assertIn('custom-depth flag write',p.restore_failure_details[3000])
        g.fail=False;p.restore();self.assertFalse(p.restore_failures)

    def test_live_update_failure_identifies_mesh_actor_and_stage(self):
        p,g,_,_,_=self.make();p.update(budget=1.)
        original_read=g.read
        def read(address,size):
            if address==3000+0x271:raise OSError(299,'live field unreadable')
            return original_read(address,size)
        g.read=read
        with self.assertRaisesRegex(RuntimeError,'mesh 0xBB8 .* update/custom-depth flag read'):
            list(p.update_steps())
        self.assertTrue(p.records[3000].applied)

    def test_guard_and_replicated_property_refused(self):
        p,g,_,_,_=self.make();rva,_=RENDER_DIRTY_GUARD;g.put(g.base+rva,b'\0'*7)
        with self.assertRaisesRegex(RuntimeError,'signature'):MobDepthProfile(p.session,p.scene)
        p.session.layouts['PrimitiveComponent']['properties'][0]['flags']|=0x20
        with self.assertRaisesRegex(RuntimeError,'replicated'):MobDepthProfile(p.session,p.scene)

    def test_budgeted_discovery_resumes_and_rechecks_detached_links(self):
        p,g,_,parents,_=self.make()
        clock=[0.]
        def tick():
            clock[0]+=.001
            return clock[0]
        with patch('mob_depth_profile.time.perf_counter',side_effect=tick):
            p.update(budget=.0015)
        self.assertIsNotNone(p.collecting)
        self.assertFalse(g.writes)
        # The attachment changed while discovery was spread across frames.
        parents[4000]=9000
        for _ in range(8):p.update(budget=1.)
        self.assertEqual(g.read(3000+0x271,1),b'\x80')
        self.assertEqual(g.read(4000+0x271,1),b'\xc0')
        self.assertNotIn(4000,p.records)

    def test_restore_cancels_pending_discovery_and_keeps_serial_guards(self):
        p,g,_,_,_=self.make()
        p.collecting=iter(p.collect_steps())
        next(p.collecting)
        p.restore()
        self.assertIsNone(p.collecting)
        self.assertIsNone(p.updating)
        self.assertFalse(g.writes)


if __name__=='__main__':unittest.main()
