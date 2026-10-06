import contextlib
import io
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from coop_diagnostics import CoopDiagnostics, player_presentation
from test_combat_profile import Memory


class ReadOnlySnapshots(unittest.TestCase):
    def test_profile_timing_preserves_return_error_and_has_bounded_storage(self):
        s=self.session();d=CoopDiagnostics(SimpleNamespace())
        s.pose=SimpleNamespace(update=Mock(return_value=False))
        original=s.pose.update
        d.track(s,None);d.track(s,None)
        with patch('coop_diagnostics.time.perf_counter',side_effect=[0.,.003]):
            self.assertIs(s.pose.update(1.),False)
        s.pose.update(1.)
        self.assertEqual(len(d.profile_times),1)
        self.assertEqual(d.profile_times['pose'][0],2)
        original.side_effect=RuntimeError('profile failure')
        with self.assertRaisesRegex(RuntimeError,'profile failure'):s.pose.update(1.)
        self.assertEqual(d.profile_times['pose'][0],3)
        health=d.runtime_health(s)
        self.assertGreaterEqual(health['profile_work_ms']['pose']['maximum'],3.)
        self.assertFalse(d.profile_times)
        self.assertFalse(s.game.writes)
    def test_health_metrics_have_constant_storage_and_reset_without_game_writes(self):
        s=self.session();d=CoopDiagnostics(SimpleNamespace())
        s.controller_profile=SimpleNamespace(swaps={1:1},axis_blocks={1:1,2:2},thresholds={1:1})
        for _ in range(20000):d.observe_frame(.002)
        health=d.runtime_health(s)
        self.assertEqual(health['frames'],20000)
        self.assertEqual(health['average_work_ms'],2.)
        self.assertEqual(health['maximum_work_ms'],2.)
        self.assertEqual(health['controller_profile.axis_blocks'],2)
        self.assertEqual(d.frame_count,0)
        self.assertEqual(d.frame_total,0.)
        self.assertEqual(s.game.writes,[])

    def session(self):
        g=Memory()
        s=SimpleNamespace(game=g,pawn=100,identities={100:'actor'},local_scope=Mock(),ensure_layout=Mock())
        s.local_scope.validate.return_value=2
        s.identity=Mock(side_effect=lambda obj:{100:'actor',200:'mesh'}[obj])
        s.pointer=Mock(side_effect=lambda obj,cls,name:200 if name=='Mesh' else 300)
        s.reflection=Mock();s.reflection.header.return_value={'name':'SK_Player_Master'}
        fields={'Role':{'type':'ByteProperty','size':1,'address':110},
                'RemoteRole':{'type':'ByteProperty','size':1,'address':111},
                'bHiddenInGame':{'type':'BoolProperty','size':1,'address':210,'bool':{'byteMask':4}},
                'bReplicates':{'type':'BoolProperty','size':1,'address':211,'bool':{'byteMask':8}}}
        s.field=Mock(side_effect=lambda obj,cls,name:fields[name])
        g.write(110,bytes([2,3]));g.write(210,bytes([0,0]))
        for offset,pointer in ((0x7A0,400),(0x7B0,500)):
            g.write(200+offset,struct.pack('<Qii',pointer,32,32));g.write(pointer,bytes([1]*32))
        g.writes.clear()
        return s

    def test_reads_visibility_without_mutation_or_actor_identifiers(self):
        s=self.session()
        result=player_presentation(s,100,'actor')
        self.assertTrue(result['local']);self.assertEqual(result['hidden_bones_per_buffer'],[0,0])
        self.assertFalse(result['mesh_hidden']);self.assertFalse(result['mesh_replicated'])
        self.assertEqual(s.game.writes,[])
        self.assertNotIn('name',result);self.assertNotIn('address',result)

    def test_recycled_player_refused_before_mesh_read(self):
        s=self.session();s.identity.return_value='recycled';s.identity.side_effect=None
        with self.assertRaises(RuntimeError):player_presentation(s,100,'actor')
        s.pointer.assert_not_called();self.assertEqual(s.game.writes,[])

    def test_replaced_buffer_is_not_reported_as_valid(self):
        s=self.session();read=s.game.read
        s.game.read=lambda a,n: struct.pack('<Qii',999,31,32) if a==200+0x7A0 else read(a,n)
        with self.assertRaises(RuntimeError):player_presentation(s,100,'actor')
        self.assertEqual(s.game.writes,[])

    def test_automatic_logs_are_bounded_and_manual_snapshot_is_forced(self):
        s=self.session();d=CoopDiagnostics(SimpleNamespace())
        scene=SimpleNamespace(players={100:'actor'})
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            d.update(s,scene,now=0);d.update(s,scene,now=1);d.update(s,scene,now=11)
            d.update(s,scene,now=12,forced=True)
        self.assertEqual(output.getvalue().count('COOP SNAPSHOT'),2)
        self.assertEqual(s.game.writes,[])

    def test_unavailable_remote_player_does_not_disable_camera(self):
        s=self.session();d=CoopDiagnostics(SimpleNamespace())
        output=io.StringIO()
        with contextlib.redirect_stdout(output):d.update(s,SimpleNamespace(players={999:'retired'}),now=0)
        self.assertIn('"unavailable_players": 1',output.getvalue())
        self.assertEqual(s.game.writes,[])
