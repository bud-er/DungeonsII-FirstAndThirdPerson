from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from roll_direction import KeyboardDodgeBindings, _KeyboardDodgeAction
from test_keyboard_movement import Memory


class KeyboardDodgeTests(unittest.TestCase):
    def fixture(self):
        g=Memory();g.base=0x10000000
        source,runtime,directional,forward=100000,200000,300000,400000
        objects={source:dict(name='IMC_DefaultKBM',**{'class':'InputMappingContext'}),
                 runtime:dict(name='EnhancedPlayerInput',**{'class':'EnhancedPlayerInput'}),
                 directional:dict(name='IA_DodgeDirectional',**{'class':'InputAction'}),
                 forward:dict(name='IA_DodgeForward',**{'class':'InputAction'})}
        ids={obj:obj for obj in objects};ids[100]=100;saved={}
        def row(address,action,key,trigger):
            raw=bytearray(0x50);pointer=trigger+0x100
            struct.pack_into('<Qii',raw,0,pointer,1,2)
            struct.pack_into('<Q',raw,0x20,action);struct.pack_into('<II',raw,0x28,key,0)
            raw[0x30:0x40]=b'native-key-cache'
            g.put(pointer,struct.pack('<Q',trigger));g.put(address,raw)
            objects[trigger]=dict(name='InputTriggerPressed',**{'class':'InputTriggerPressed'})
            ids[trigger]=trigger;saved[address]=bytes(raw)
        row(600000,directional,82,500000)
        row(600080,forward,5,501000)
        row(610000,directional,82,502000)
        row(610080,directional,82,503000)
        row(610160,forward,5,504000)
        row(610240,forward,128,505000)
        g.put(source+0x50,struct.pack('<Qii',600000,2,2))
        g.put(runtime+0x50,struct.pack('<Qii',610000,4,4))
        g.put(directional+0x48,b'\x02');g.put(forward+0x48,b'\0')
        rva,expected=KeyboardDodgeBindings.FORWARD_GUARD;g.put(g.base+rva,expected)
        s=SimpleNamespace(game=g,pc=100,pawn=200,local_scope=SimpleNamespace(validate=Mock()),
            ensure_layout=Mock(),identity=lambda o:ids[o],object_retired=lambda o,i:ids.get(o)!=i,
            objects=[dict(address=source,flags=0,**objects[source])],is_a=lambda cls,name:cls==name,
            pointer=lambda *args:runtime,
            field=lambda obj,cls,name:dict(address=obj+(0x48 if name=='ValueType' else 0x50),size=1 if name=='ValueType' else 16),
            reflection=SimpleNamespace(headers={},header=lambda o:objects[o],
                name=lambda index,number:{82:'R',5:'ThumbMouseButton2',128:'Gamepad_RightThumbstick'}[index]),
            layouts={'EnhancedActionKeyMapping':dict(size=0x50,properties=[
                dict(name=name,offset=offset,size=size) for name,offset,size in
                (('Action',0x20,8),('Key',0x28,24),('Triggers',0,16),('Modifiers',0x10,16))])})
        return s,g,ids,objects,saved

    def profile(self):
        s,g,ids,objects,saved=self.fixture()
        return KeyboardDodgeBindings(s),g,ids,objects,saved

    def test_only_source_and_duplicate_runtime_r_actions_change(self):
        p,g,_,_,saved=self.profile()
        self.assertEqual(len(p.records),3);self.assertEqual(g.writes,[])
        self.assertTrue(p.update(enabled=True,key_down=False))
        self.assertEqual({a for a,v in g.writes},{600032,610032,610112})
        for address,raw in saved.items():
            current=g.read(address,0x50)
            self.assertEqual(current[:0x20],raw[:0x20])
            self.assertEqual(current[0x28:],raw[0x28:])
        self.assertEqual(g.read(600080,0x50),saved[600080])
        self.assertEqual(g.read(610160,0x50),saved[610160])
        self.assertEqual(g.read(610240,0x50),saved[610240])  # R3 remains instant/native.

    def test_first_person_and_unknown_or_held_r_never_start_remapping(self):
        for enabled,down in ((False,False),(True,None),(True,True)):
            p,g,_,_,_=self.profile()
            self.assertTrue(p.update(enabled=enabled,key_down=down))
            self.assertFalse(p.route_forward);self.assertEqual(g.writes,[])

    def test_held_r_during_mode_change_keeps_action_until_release(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False);g.writes.clear()
        for _ in range(3):p.update(enabled=False,key_down=True)
        self.assertTrue(p.route_forward);self.assertEqual(g.writes,[])
        p.update(enabled=False,key_down=False)
        self.assertFalse(p.route_forward)
        self.assertTrue(all(v==struct.pack('<Q',p.directional) for a,v in g.writes))
        g.writes.clear();p.update(enabled=False,key_down=False);self.assertEqual(g.writes,[])

    def test_cooldown_presses_do_not_rewrite_actions_triggers_or_roll_state(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False);g.writes.clear()
        for down in (True,True,False,True,False):p.update(enabled=True,key_down=down)
        self.assertEqual(g.writes,[])

    def test_context_rebuild_inherited_forward_r_restores_from_new_array(self):
        p,g,_,_,saved=self.profile();p.update(enabled=True,key_down=False)
        data=g.read(610000,4*0x50);g.put(620000,data)
        g.put(200000+0x50,struct.pack('<Qii',620000,4,4))
        p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(620032,8),struct.pack('<Q',p.directional))
        self.assertEqual(g.read(620112,8),struct.pack('<Q',p.directional))
        self.assertEqual(g.read(620160,0x50),saved[610160])

    def test_live_changed_metadata_keeps_restoration_failure_until_safe_retry(self):
        p,g,_,_,saved=self.profile();p.update(enabled=True,key_down=False)
        # Change cached FKey bytes on a live source row still containing ours.
        g.put(600000+0x30,b'changed!');g.writes.clear()
        p.restore();self.assertTrue(p.restore_failures);self.assertTrue(p.applied)
        self.assertEqual(g.read(600032,8),struct.pack('<Q',p.forward))
        self.assertNotIn(600032,{a for a,v in g.writes})
        g.put(600000+0x30,saved[600000][0x30:0x38]);p.restore()
        self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(600032,8),struct.pack('<Q',p.directional))

    def test_unknown_owner_lifetime_never_writes_owned_row(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False);p.collect=Mock()
        p.session.object_retired=lambda *args:None;g.writes.clear();p.restore()
        self.assertTrue(p.restore_failures);self.assertEqual(g.writes,[])

    def test_retired_owner_and_replaced_action_are_not_overwritten(self):
        for kind in ('retired','action'):
            p,g,ids,objects,_=self.profile();p.update(enabled=True,key_down=False);p.collect=Mock()
            if kind=='retired':ids[100000]=999
            else:
                objects[999]=dict(name='CustomAction',**{'class':'InputAction'})
                g.put(600032,struct.pack('<Q',999))
            g.writes.clear();p.restore()
            self.assertFalse(p.restore_failures)
            self.assertNotIn(600032,{a for a,v in g.writes})

    def test_partial_write_failure_retains_restoration_ownership(self):
        p,g,_,_,_=self.profile();original=g.write
        def partial(a,v):
            original(a,v)
            raise OSError('write result unknown')
        g.write=partial
        with self.assertRaises(OSError):p.update(enabled=True,key_down=False)
        self.assertTrue(p.applied)
        g.write=original;p.restore();self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(600032,8),struct.pack('<Q',p.directional))

    def test_unsupported_native_guard_type_or_custom_trigger_fail_before_writes(self):
        for kind in ('guard','type','trigger','modifiers','custom key'):
            s,g,_,objects,_=self.fixture()
            if kind=='guard':g.put(g.base+KeyboardDodgeBindings.FORWARD_GUARD[0],b'\x90')
            elif kind=='type':g.put(400000+0x48,b'\x02')
            elif kind=='trigger':objects[500000]['class']='InputTriggerHold'
            elif kind=='modifiers':g.put(600000+0x18,struct.pack('<i',1))
            else:g.put(600000+0x28,struct.pack('<II',5,0))
            with self.subTest(kind=kind):
                with self.assertRaises(RuntimeError):KeyboardDodgeBindings(s)
                self.assertEqual(g.writes,[])

    def test_in_place_trigger_array_replacement_blocks_owned_mapping_writes(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False)
        g.put(500000+0x100,struct.pack('<Q',501000));g.writes.clear()
        with self.assertRaisesRegex(RuntimeError,'metadata changed'):
            p.update(enabled=True,key_down=False)
        p.restore();self.assertTrue(p.restore_failures)
        self.assertNotIn(600032,{a for a,v in g.writes})

    def test_runtime_rebuilt_during_source_restore_is_recollected_and_restored(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False)
        write=g.write;rebuilt=[]
        def rebuild(a,v):
            if a==600032 and v==struct.pack('<Q',p.directional) and not rebuilt:
                g.put(620000,g.read(610000,4*0x50))
                g.put(200000+0x50,struct.pack('<Qii',620000,4,4));rebuilt.append(True)
            write(a,v)
        g.write=rebuild;p.restore()
        self.assertFalse(p.restore_failures);self.assertEqual(len(rebuilt),1)
        self.assertEqual(g.read(620032,8),struct.pack('<Q',p.directional))
        self.assertEqual(g.read(620112,8),struct.pack('<Q',p.directional))

    def test_leaving_third_person_recollects_runtime_rebuilt_during_source_restore(self):
        p,g,_,_,_=self.profile();p.update(enabled=True,key_down=False)
        write=g.write;rebuilt=[]
        def rebuild(a,v):
            if a==600032 and v==struct.pack('<Q',p.directional) and not rebuilt:
                g.put(620000,g.read(610000,4*0x50))
                g.put(200000+0x50,struct.pack('<Qii',620000,4,4));rebuilt.append(True)
            write(a,v)
        g.write=rebuild;p.update(enabled=False,key_down=False)
        self.assertFalse(p.applied);self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(620032,8),struct.pack('<Q',p.directional))
        self.assertEqual(g.read(620112,8),struct.pack('<Q',p.directional))

    def test_unused_optional_route_restores_without_reading_retired_input(self):
        p,g,_,_,_=self.profile()
        p.session.pointer=Mock(side_effect=OSError('menu replaced input'))
        p.restore();p.update(enabled=False,key_down=False)
        self.assertFalse(p.restore_failures);self.assertEqual(g.writes,[])
        p.session.pointer.assert_not_called()

    def test_new_possession_blocks_changes_before_any_mapping_write(self):
        p,g,_,_,_=self.profile()
        p.session.local_scope.validate.side_effect=RuntimeError('possession changed')
        with self.assertRaisesRegex(RuntimeError,'possession changed'):p.update(enabled=True,key_down=False)
        self.assertEqual(g.writes,[])

    def test_recycled_source_cannot_be_recaptured_as_a_new_trusted_context(self):
        p,g,ids,_,_=self.profile();p.update(enabled=True,key_down=False)
        g.put(630000,g.read(600000,2*0x50))
        g.put(100000+0x50,struct.pack('<Qii',630000,2,2));ids[100000]=999
        g.writes.clear()
        with self.assertRaisesRegex(RuntimeError,'source identity changed'):
            p.update(enabled=True,key_down=False)
        self.assertEqual(g.writes,[])

    def test_restoration_never_follows_a_recycled_controller_player_input_pointer(self):
        p,g,ids,_,_=self.profile();p.update(enabled=True,key_down=False)
        ids[100]=999
        p.session.pointer=Mock(side_effect=AssertionError('must not follow recycled controller'))
        p.restore()
        p.session.pointer.assert_not_called();self.assertFalse(p.restore_failures)
        self.assertEqual(g.read(610032,8),struct.pack('<Q',p.directional))
