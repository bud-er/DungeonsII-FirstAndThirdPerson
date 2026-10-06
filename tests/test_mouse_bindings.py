from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mouse_bindings import KeyAlias, MouseBindings
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
        profile.click_move_guard = self.guard()
        return profile, record, game

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
        p.click_move_guard=self.guard()
        g.fail=True;p.restore();self.assertEqual(p.restore_failures,{1});self.assertTrue(p.applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures);self.assertFalse(p.applied)

    def test_rebuilt_mouse_alias_is_applied_before_the_collection_deadline(self):
        r,g,_=self.record();r.apply()
        p=MouseBindings.__new__(MouseBindings)
        p.session,p.game=r.profile.session,g
        p.applied,p.interact,p.restore_failures,p.next_collect=True,False,set(),float('inf')
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
