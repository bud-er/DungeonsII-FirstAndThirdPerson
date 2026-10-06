from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cursor_profile import CursorProfile, INVISIBLE
from test_keyboard_movement import Memory


class CursorTests(unittest.TestCase):
    def profile(self):
        p=CursorProfile.__new__(CursorProfile);g=p.game=Memory()
        ids={100:1,200:2};owner=[200]
        p.session=SimpleNamespace(pc=100,identities=ids,identity=lambda o:ids[o],
                                  pointer=lambda *args:owner[0])
        p.session.reflection=SimpleNamespace(headers={},header=lambda o:dict(outer=100,class_name='MouseTargeter',**{'class':'MouseTargeter'}))
        p.session.is_a=lambda cls,name:cls==name
        p.targeter,p.identity,p.address,p.saved=200,2,200+0x112,bytes((2,0))
        p.applied,p.restore_failures=False,set();g.put(p.address,p.saved)
        return p,g,ids,owner

    def test_only_native_local_cursor_override_changes_and_restores(self):
        p,g,_,_=self.profile();p.update();p.update()
        self.assertEqual(g.writes,[(p.address,INVISIBLE)])
        p.restore();self.assertEqual(g.read(p.address,2),bytes((2,0)))
        self.assertFalse(p.applied);self.assertFalse(p.restore_failures)

    def test_bow_targeter_replacement_restores_old_and_captures_new_baseline(self):
        p,g,ids,owner=self.profile();p.update();g.writes.clear()
        ids[777]=3;owner[0]=777;g.put(777+0x112,bytes((5,1)))
        p.update()
        self.assertEqual(g.writes,[(200+0x112,bytes((2,0))),(777+0x112,INVISIBLE)])
        p.restore()
        self.assertEqual(g.read(777+0x112,2),bytes((5,1)))
        self.assertFalse(p.restore_failures)

    def test_same_address_new_serial_never_receives_old_override(self):
        p,g,ids,_=self.profile();p.update();g.writes.clear()
        ids[200]=99;g.put(p.address,bytes((5,0)))
        p.update();p.restore()
        self.assertEqual(g.writes,[(p.address,INVISIBLE),(p.address,bytes((5,0)))])
        self.assertFalse(p.restore_failures)

    def test_temporary_null_targeter_keeps_view_without_cursor_writes(self):
        p,g,_,owner=self.profile();p.update();owner[0]=0
        self.assertFalse(p.update());g.writes.clear()
        self.assertFalse(p.update());self.assertEqual(g.writes,[])

    def test_foreign_targeter_is_refused_without_writing_it(self):
        p,g,ids,owner=self.profile();p.update();g.writes.clear()
        ids[777]=3;owner[0]=777
        p.session.reflection.header=lambda obj:{'outer':100 if obj==200 else 555,'class':'MouseTargeter'}
        with self.assertRaisesRegex(RuntimeError,'not owned'):p.update()
        self.assertEqual(g.writes,[(200+0x112,bytes((2,0)))])

    def test_proven_retirement_skips_freed_object_but_read_failure_remains_blocked(self):
        p,g,_,_=self.profile();p.update();g.writes.clear()
        p.session.object_retired=lambda *args:True
        p.session.identity=lambda obj:(_ for _ in ()).throw(OSError('freed'))
        p.restore();self.assertFalse(p.restore_failures);self.assertEqual(g.writes,[])
        p.applied=True
        p.session.object_retired=lambda *args:(_ for _ in ()).throw(OSError('unreadable slot'))
        p.restore();self.assertTrue(p.restore_failures);self.assertTrue(p.applied)
        self.assertEqual(g.writes,[])

    def test_invalid_enum_or_override_flag_blocks_updates(self):
        for value in (bytes((10,0)),bytes((2,2))):
            p,g,_,_=self.profile();g.put(p.address,value)
            with self.assertRaises(RuntimeError):p.update()
            self.assertEqual(g.writes,[])

    def test_failed_restore_can_retry(self):
        p,g,_,_=self.profile();p.update();g.fail=True;p.restore()
        self.assertTrue(p.applied);self.assertTrue(p.restore_failures)
        g.fail=False;p.restore();self.assertFalse(p.applied);self.assertFalse(p.restore_failures)

    def test_native_menu_override_is_preserved_during_handoff(self):
        p,g,_,_=self.profile();p.update();g.writes.clear()
        native=bytes((5,1))
        g.put(p.address,native)
        p.restore()
        self.assertEqual(g.read(p.address,2),native)
        self.assertEqual(g.writes,[])
        self.assertFalse(p.applied);self.assertFalse(p.restore_failures)

    def test_unknown_override_is_not_treated_as_successful_restoration(self):
        p,g,_,_=self.profile();p.update();g.writes.clear()
        g.put(p.address,bytes((250,1)))
        p.restore()
        self.assertTrue(p.applied);self.assertTrue(p.restore_failures)
        self.assertEqual(g.writes,[])
