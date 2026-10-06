from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mouse_bindings import KeyAlias, MouseBindings, AirAction, AirborneClickGate
from input_mapping_snapshot import MappingRebuilt
from test_keyboard_movement import Memory


class MouseActionTests(unittest.TestCase):
    def guard(self):
        return SimpleNamespace(update=Mock(return_value=True), restore=Mock(), restore_failures=set())

    def profile(self):
        record, game, _ = self.record()
        profile = MouseBindings.__new__(MouseBindings)
        profile.session, profile.game = record.profile.session, game
        profile.applied, profile.interact = False, False
        profile.restore_failures, profile.next_collect = set(), 0.
        profile.records, profile.collect = {1: record}, Mock()
        self.air_defaults(profile)
        profile.click_move_guard = self.guard()
        return profile, record, game

    def air_defaults(self, profile):
        profile.air_records = {}
        profile.air_available = True
        profile.air_gate = SimpleNamespace(falling=Mock(return_value=False))
        profile.route_air, profile.primary_down = False, None

    def record(self, inherited=False):
        g=Memory();ids={100:1,200:2,300:3}
        s=SimpleNamespace(identity=lambda o:ids[o])
        p=SimpleNamespace(session=s,game=g)
        descriptor=struct.pack('<Qii',500,1,1);g.put(400,descriptor)
        raw=bytearray(range(0x50));struct.pack_into('<Q',raw,0x20,200)
        raw[0x28:0x30]=b'mousekey' if inherited else b'shiftkey'
        g.put(500,raw)
        return KeyAlias(p,100,400,descriptor,500,b'shiftkey',b'mousekey'),g,ids

    def test_force_attack_alias_keeps_primary_action_cache_triggers_and_modifiers(self):
        r,g,_=self.record();old=g.read(500,0x50);self.assertTrue(r.apply())
        self.assertEqual(g.writes,[(500+0x28,b'mousekey')])
        self.assertEqual(g.read(500,0x28),old[:0x28]);self.assertEqual(g.read(500+0x30,0x20),old[0x30:])
        g.put(500+0x40,b'\x02');self.assertTrue(r.restore())
        self.assertEqual(g.read(500+0x28,8),b'shiftkey');self.assertEqual(g.read(500+0x40,1),b'\x02')

    def test_rebuilt_context_inheriting_replacement_restores_without_an_apply(self):
        r,g,_=self.record(inherited=True);self.assertTrue(r.applied);self.assertTrue(r.restore())
        self.assertEqual(g.read(500+0x28,8),b'shiftkey')

    def test_shift_click_removes_only_force_attack_alias_and_restores_after_release(self):
        r,g,_=self.record();r.apply();r.apply(interact=True)
        self.assertEqual(g.read(500+0x28,8),bytes(8))
        self.assertEqual(g.read(500+0x20,8),struct.pack('<Q',200))
        self.assertTrue(r.apply());self.assertEqual(g.read(500+0x28,8),b'mousekey')
        self.assertTrue(r.restore());self.assertEqual(g.read(500+0x28,8),b'shiftkey')

    def test_recycled_owner_action_changed_key_and_descriptor_are_not_written(self):
        for change in ('owner','action','key','array','custom_action','cache'):
            r,g,ids=self.record();r.apply()
            if change=='owner':ids[100]=99
            elif change=='action':ids[200]=99
            elif change=='key':g.put(500+0x28,b'changed!')
            elif change=='array':g.put(400,struct.pack('<Qii',600,1,1))
            elif change=='custom_action':g.put(500+0x20,struct.pack('<Q',777))
            elif change=='cache':g.put(500+0x30,b'changed!')
            g.writes.clear()
            with self.subTest(change=change):
                self.assertFalse(r.apply());self.assertTrue(r.restore());self.assertEqual(g.writes,[])

    def test_failed_restore_is_reported_and_can_retry(self):
        r,g,_=self.record();r.apply()
        p=MouseBindings.__new__(MouseBindings);p.applied=True;p.records={1:r};p.restore_failures=set();p.collect=lambda:None
        self.air_defaults(p)
        p.click_move_guard=self.guard()
        g.fail=True;p.restore();self.assertEqual(p.restore_failures,{1});self.assertTrue(p.applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures);self.assertFalse(p.applied)

    def test_rebuilt_mouse_alias_is_applied_before_the_collection_deadline(self):
        r,g,_=self.record();r.apply()
        p=MouseBindings.__new__(MouseBindings)
        p.session,p.game=r.profile.session,g
        p.applied,p.interact,p.restore_failures,p.next_collect=True,False,set(),float('inf')
        self.air_defaults(p)
        p.click_move_guard=self.guard()
        p.records={1:r}
        g.put(900,g.read(500,0x50));g.put(900+0x28,b'shiftkey')
        descriptor=struct.pack('<Qii',900,1,1);g.put(400,descriptor)
        def collect():
            p.records[2]=KeyAlias(p,100,400,descriptor,900,b'shiftkey',b'mousekey')
        p.collect=collect;p.update()
        self.assertEqual(g.read(900+0x28,8),b'mousekey')
        self.assertEqual(len(p.records),1)
        p.restore();self.assertEqual(g.read(900+0x28,8),b'shiftkey')

    def test_release_path_is_suppressed_before_mapping_collection_and_alias_write(self):
        p, _, game = self.profile(); events = []
        p.click_move_guard.update.side_effect = lambda **kw: events.append('guard') or True
        p.collect.side_effect = lambda: events.append('collect')
        write = game.write
        def observed_write(address, value):
            events.append('alias'); write(address, value)
        game.write = observed_write
        p.update()
        self.assertEqual(events, ['guard', 'collect', 'alias'])

    def test_active_native_path_defers_alias_without_losing_restore_coverage(self):
        p, _, game = self.profile(); p.click_move_guard.update.return_value = False
        self.assertIs(p.update(), False)
        p.collect.assert_not_called(); self.assertEqual(game.writes, [])
        self.assertTrue(p.applied)
        p.restore(); p.click_move_guard.restore.assert_called_once()
        self.assertFalse(p.applied)

    def test_mapping_rebuild_after_suppression_is_restored_without_alias_application(self):
        p, _, game = self.profile(); p.collect.side_effect = MappingRebuilt('changed')
        self.assertIs(p.update(), False)
        self.assertEqual(game.writes, []); self.assertTrue(p.applied)
        p.collect.side_effect = None
        p.restore(); p.click_move_guard.restore.assert_called_once()
        self.assertFalse(p.restore_failures); self.assertFalse(p.applied)

    def test_partial_guard_failure_remains_owned_and_gets_restored(self):
        p, _, game = self.profile()
        p.click_move_guard.update.side_effect = OSError('partial guard write')
        with self.assertRaisesRegex(OSError, 'partial guard write'): p.update()
        self.assertTrue(p.applied); self.assertEqual(game.writes, [])
        p.restore(); p.click_move_guard.restore.assert_called_once()
        self.assertFalse(p.applied)

    def test_shift_keeps_guard_active_and_removes_only_force_attack_alias(self):
        p, _, game = self.profile(); p.update(); p.set_input(True); p.update()
        p.click_move_guard.update.assert_called_with(interact=True)
        p.click_move_guard.restore.assert_not_called()
        self.assertEqual(game.read(500+0x28,8), bytes(8))
        p.set_input(False); p.update()
        self.assertEqual(game.read(500+0x28,8), b'mousekey')

    def test_restore_releases_guard_even_when_alias_collection_fails(self):
        p, _, game = self.profile(); p.update()
        p.collect.side_effect = OSError('mapping unavailable')
        p.restore(); p.click_move_guard.restore.assert_called_once()
        self.assertEqual(game.read(500+0x28,8), b'shiftkey')
        self.assertEqual(p.restore_failures, {'mouse context discovery'})
        self.assertTrue(p.applied)
        p.collect.side_effect = None; p.restore()
        self.assertFalse(p.restore_failures); self.assertFalse(p.applied)

    def test_guard_restore_failure_blocks_clean_handoff_and_can_retry(self):
        p, _, _ = self.profile()
        p.click_move_guard.restore_failures = {'readback failed'}
        p.restore()
        self.assertEqual(p.restore_failures, {'native click path'}); self.assertTrue(p.applied)
        p.click_move_guard.restore_failures.clear(); p.restore()
        self.assertFalse(p.restore_failures); self.assertFalse(p.applied)

    def test_native_keys_are_restored_before_release_path_is_enabled(self):
        p, _, game = self.profile(); p.update()
        def restore_guard(): self.assertEqual(game.read(500+0x28,8), b'shiftkey')
        p.click_move_guard.restore.side_effect = restore_guard
        p.restore(); p.click_move_guard.restore.assert_called_once()

    def with_air_action(self):
        p, root, g = self.profile()
        p.primary_action, p.air_action = 200, 300
        p.primary_identity, p.air_identity = 2, 3
        p.session.object_retired = lambda obj,identity:p.session.identity(obj)!=identity
        descriptor = struct.pack('<Qii', 700, 1, 1)
        g.put(600, descriptor)
        raw = bytearray(0x50)
        struct.pack_into('<Q', raw, 0x20, 200)
        raw[0x28:0x30] = b'mousekey'
        raw[0x30:0x40] = b'cached-fkey-data'
        g.put(700, raw)
        air = AirAction(p, 100, 600, descriptor, 700, bytes(raw))
        p.air_records = {2: air}
        return p, root, air, g

    def test_airborne_routes_only_mouse_primary_and_releases_ground_root_first(self):
        p, _, _, g = self.with_air_action()
        p.set_input(False, primary_down=False); p.update(); g.writes.clear()
        before = g.read(700, 0x50)
        p.air_gate.falling.return_value = True
        p.update()
        self.assertEqual(g.writes, [(500+0x28, bytes(8)), (700+0x20, struct.pack('<Q', 300))])
        self.assertEqual(g.read(700, 0x20), before[:0x20])
        self.assertEqual(g.read(700+0x28, 0x28), before[0x28:])
        p.click_move_guard.update.assert_called_with(interact=False)

    def test_held_ground_click_does_not_become_a_slam_on_takeoff(self):
        p, _, _, g = self.with_air_action()
        p.set_input(False, primary_down=True)
        p.air_gate.falling.return_value = True
        p.update()
        self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 200))
        p.air_gate.falling.assert_not_called()
        p.set_input(False, primary_down=False); p.update()
        self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 300))

    def test_held_air_click_does_not_start_melee_on_landing_and_restores_on_release(self):
        p, _, _, g = self.with_air_action()
        p.air_gate.falling.return_value = True
        p.set_input(False, primary_down=False); p.update()
        p.set_input(False, primary_down=True)
        p.air_gate.falling.return_value = False; p.update()
        self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 300))
        p.set_input(False, primary_down=False); g.writes.clear(); p.update()
        self.assertEqual(g.writes, [(700+0x20, struct.pack('<Q', 200)), (500+0x28, b'mousekey')])

    def test_shift_ground_interaction_and_native_heavy_shortcuts_are_not_rebound(self):
        p, _, _, g = self.with_air_action()
        g.put(900, b'native Q and side mouse mappings')
        p.set_input(True, primary_down=False); p.update()
        self.assertEqual(g.read(500+0x28, 8), bytes(8))
        self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 200))
        p.air_gate.falling.return_value = True; p.update()
        self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 300))
        self.assertEqual(g.read(900, len(b'native Q and side mouse mappings')), b'native Q and side mouse mappings')

    def test_capture_skips_native_slam_shortcuts_and_tracks_inherited_lmb_action(self):
        p, _, _, g = self.with_air_action()
        p.root_action, p.root_identity = 400, 4
        p.session.identity = lambda o:{100:1,200:2,300:3,400:4}[o]
        p.air_records = {}; p.records = {}
        p.original, p.desired = b'shiftkey', b'mousekey'
        descriptor=struct.pack('<Qii',1000,4,4); g.put(600,descriptor)
        rows=[]
        for i,(action,key) in enumerate(((300,b'mousekey'),(300,b'q-key---'),
                                         (300,b'side-key'),(400,b'shiftkey'))):
            raw=bytearray(0x50); struct.pack_into('<Q',raw,0x20,action)
            raw[0x28:0x30]=key; address=1000+i*0x50; g.put(address,raw)
            rows.append(dict(raw=bytes(raw),address=address))
        p.rows=Mock(return_value=(600,descriptor,rows))
        p.capture(100,'InputMappingContext','Mappings')
        self.assertEqual(len(p.air_records),1); self.assertEqual(len(p.records),1)
        p.applied=True; p.restore()
        self.assertEqual(g.read(1000+0x20,8),struct.pack('<Q',200))
        self.assertEqual(g.read(1000+0x50,0x50),rows[1]['raw'])
        self.assertEqual(g.read(1000+0xa0,0x50),rows[2]['raw'])

    def test_rebuilt_air_record_is_collected_immediately_and_gets_current_route(self):
        p, _, air, g = self.with_air_action()
        p.set_input(False,primary_down=False); p.air_gate.falling.return_value=True; p.update()
        p.next_collect=float('inf')
        raw=bytearray(g.read(700,0x50));struct.pack_into('<Q',raw,0x20,200)
        descriptor=struct.pack('<Qii',900,1,1);g.put(900,raw);g.put(600,descriptor)
        def collect():
            p.air_records[3]=AirAction(p,100,600,descriptor,900,bytes(raw))
        p.collect.side_effect=collect; p.update()
        self.assertEqual(g.read(900+0x20,8),struct.pack('<Q',300))
        self.assertEqual(len(p.air_records),1)
        self.assertNotIn(air,p.air_records.values())

    def test_menu_handoff_restores_air_action_then_keys_then_click_guard(self):
        p, _, _, g = self.with_air_action()
        p.air_gate.falling.return_value = True
        p.set_input(False, primary_down=False); p.update()
        def restored():
            self.assertEqual(g.read(700+0x20, 8), struct.pack('<Q', 200))
            self.assertEqual(g.read(500+0x28, 8), b'shiftkey')
        p.click_move_guard.restore.side_effect = restored
        p.restore()
        self.assertFalse(p.route_air); self.assertIsNone(p.primary_down)
        self.assertFalse(p.restore_failures)

    def test_rebuilt_air_context_inherits_action_and_restores_even_without_update(self):
        p, _, air, g = self.with_air_action(); air.apply(True)
        descriptor = struct.pack('<Qii', 900, 1, 1)
        raw = g.read(700, 0x50); g.put(900, raw); g.put(600, descriptor)
        replacement = AirAction(p, 100, 600, descriptor, 900, raw)
        self.assertTrue(replacement.applied)
        g.writes.clear(); self.assertTrue(air.restore()); self.assertEqual(g.writes, [])
        self.assertTrue(replacement.restore())
        self.assertEqual(g.read(900+0x20, 8), struct.pack('<Q', 200))

    def test_air_action_refuses_recycled_owner_action_changed_key_and_cache(self):
        for change in ('owner','primary','air','key','array','action','cache'):
            p, _, a, g = self.with_air_action(); a.apply(True)
            if change in ('owner','primary','air'):
                changed = {'owner':100,'primary':200,'air':300}[change]
                p.session.identity = lambda o: 999 if o==changed else {100:1,200:2,300:3}[o]
            elif change=='key':g.put(700+0x28,b'changed!')
            elif change=='array':g.put(600,struct.pack('<Qii',900,1,1))
            elif change=='action':g.put(700+0x20,struct.pack('<Q',777))
            else:g.put(700+0x30,b'changed!')
            g.writes.clear()
            with self.subTest(change=change):
                self.assertFalse(a.apply(False))
                self.assertEqual(a.restore(),change in ('owner','array','action'))
                self.assertEqual(g.writes,[])

    def test_air_restore_write_failure_keeps_ownership_for_retry(self):
        p, _, a, g = self.with_air_action(); a.apply(True); p.applied=True
        g.fail=True; p.restore()
        self.assertIn(2,p.restore_failures); self.assertTrue(a.applied)
        g.fail=False; p.restore()
        self.assertFalse(p.restore_failures); self.assertFalse(a.applied)

    def test_live_air_metadata_drift_blocks_handoff_without_overwriting_and_can_retry(self):
        p,_,a,g=self.with_air_action();a.apply(True);p.applied=True
        original_cache=g.read(700+0x30,8);g.put(700+0x30,b'changed!');g.writes.clear()
        p.restore()
        self.assertEqual(p.restore_failures,{2});self.assertTrue(p.applied);self.assertTrue(a.applied)
        self.assertEqual(g.read(700+0x20,8),struct.pack('<Q',300));self.assertEqual(g.writes,[])
        g.put(700+0x30,original_cache);p.restore()
        self.assertFalse(p.restore_failures);self.assertFalse(p.applied)
        self.assertEqual(g.read(700+0x20,8),struct.pack('<Q',200))

    def test_unknown_air_owner_lifetime_is_a_restoration_failure(self):
        p,_,a,g=self.with_air_action();a.apply(True);p.applied=True;g.writes.clear()
        p.session.object_retired=lambda *args:None
        p.restore()
        self.assertEqual(p.restore_failures,{2});self.assertTrue(a.applied);self.assertEqual(g.writes,[])

    def test_movement_gate_only_accepts_native_falling_and_current_local_owner(self):
        g=Memory(); ids={300:3}
        scope=SimpleNamespace(validate=Mock())
        s=SimpleNamespace(game=g,pawn=100,local_scope=scope,identity=lambda o:ids[o],
            pointer=lambda *args:300,reflection=SimpleNamespace(header=lambda o:{'outer':100}))
        gate=AirborneClickGate.__new__(AirborneClickGate)
        gate.session,gate.game,gate.movement,gate.identity,gate.address=s,g,300,3,800
        for mode in range(7):
            g.put(800,bytes([mode])); self.assertEqual(gate.falling(),mode==3)
        ids[300]=4
        with self.assertRaisesRegex(RuntimeError,'ownership'):gate.falling()
        self.assertEqual(g.writes,[])

    def constructor_fixture(self, unsupported=None):
        g=Memory(); rows=[]
        for i,(action,key,an,kn) in enumerate(((200,b'mousekey','IA_KBM_PrimaryAction','LeftMouseButton'),
                (300,b'shiftkey','IA_RootPlayer','LeftShift'),(400,b'q-key---','IA_HeavyJumpAttack','Q'))):
            if unsupported=='missing heavy' and i==2:continue
            raw=bytearray(0x50);struct.pack_into('<Q',raw,0x20,action);raw[0x28:0x30]=key
            if unsupported=='modifiers' and i==2:raw[0]=1
            if unsupported=='missing root' and i==1:an='CustomRoot'
            address=1000+i*0x50;g.put(address,raw)
            rows.append(dict(address=address,raw=bytes(raw),action=an,key=kn))
            g.put(2000+action,b'\x01' if unsupported=='type' and i==2 else b'\0')
        descriptor=struct.pack('<Qii',1000,3,3);g.put(600,descriptor)
        s=SimpleNamespace(game=g,ensure_layout=Mock(),is_a=lambda cls,name:cls==name,
            identity=lambda o:o,objects=[dict(address=100,name='IMC_DefaultKBM',flags=0,**{'class':'InputMappingContext'})],
            field=lambda obj,cls,name:dict(size=1,address=2000+obj),pc=50,pointer=lambda *args:0,
            reflection=SimpleNamespace(name=lambda *args:'None'),
            layouts={'EnhancedActionKeyMapping':{'size':0x50,'properties':[
                dict(name='Action',offset=0x20,size=8),dict(name='Key',offset=0x28,size=24)]},
                'Key':{'size':24,'properties':[dict(name='KeyName',offset=0,size=8)]}})
        gate=SimpleNamespace(falling=Mock(return_value=True))
        return s,g,rows,descriptor,gate

    def test_optional_air_setup_failure_retains_valid_ground_controls_without_air_writes(self):
        for failure in ('missing heavy','modifiers','type','falling layout'):
            s,g,rows,descriptor,gate=self.constructor_fixture(failure)
            with self.subTest(failure=failure), \
                    patch('mouse_bindings.ClickMoveGuard',return_value=self.guard()), \
                    patch('mouse_bindings.AirborneClickGate',return_value=gate,
                          side_effect=RuntimeError('Unsupported airborne movement mode') if failure=='falling layout' else None), \
                    patch.object(MouseBindings,'rows',return_value=(600,descriptor,rows)), \
                    patch('builtins.print') as log:
                p=MouseBindings(s)
                self.assertFalse(p.air_available);self.assertFalse(p.air_records);self.assertEqual(g.writes,[])
                log.assert_called_once()
                self.assertIn('Airborne mouse routing unavailable:',log.call_args.args[0])
                self.assertTrue(p.air_error)
                p.set_input(False,primary_down=False);p.update()
                self.assertEqual(g.writes,[(1000+0x50+0x28,b'mousekey')])
                self.assertEqual(g.read(1000+0x20,8),struct.pack('<Q',200))
                p.set_input(True,primary_down=False);p.update()
                self.assertEqual(g.read(1000+0x50+0x28,8),bytes(8))
                gate.falling.assert_not_called();p.restore()
                self.assertFalse(p.restore_failures)
                self.assertEqual(g.read(1000+0x50+0x28,8),b'shiftkey')
                log.assert_called_once()

    def test_optional_air_setup_does_not_relax_mandatory_ground_mapping_validation(self):
        s,g,rows,descriptor,gate=self.constructor_fixture('missing root')
        with patch('mouse_bindings.ClickMoveGuard',return_value=self.guard()), \
                patch('mouse_bindings.AirborneClickGate',return_value=gate) as air, \
                patch.object(MouseBindings,'rows',return_value=(600,descriptor,rows)):
            with self.assertRaisesRegex(RuntimeError,'supported pair'):MouseBindings(s)
            air.assert_not_called();self.assertEqual(g.writes,[])

    def test_runtime_air_gate_error_is_not_silenced_after_mapping_write(self):
        p,_,_,g=self.with_air_action()
        p.air_gate.falling.return_value=True
        p.set_input(False,primary_down=False);p.update()
        p.air_gate.falling.side_effect=RuntimeError('Airborne movement ownership changed')
        with self.assertRaisesRegex(RuntimeError,'ownership changed'):p.update()
        self.assertTrue(p.applied)
        self.assertEqual(g.read(700+0x20,8),struct.pack('<Q',300))
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(700+0x20,8),struct.pack('<Q',200))
