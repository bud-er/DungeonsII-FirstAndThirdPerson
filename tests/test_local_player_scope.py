from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from local_player_scope import LocalPlayerScope
from external_camera import CameraSession


class LocalScopeTests(unittest.TestCase):
    def session(self, role=3):
        s = SimpleNamespace(pc=1, pawn=2, identities={1:'pc', 2:'pawn'})
        identities = {1:'pc', 2:'pawn', 3:'local player'}
        s.identity = Mock(side_effect=lambda p: identities[p])
        s.remember = lambda p, kind: s.identities.update({p:s.identity(p)})
        s.ensure_layout = Mock()
        s.reflection = Mock()
        s.reflection.header.return_value = {'class':100}
        s.is_a = Mock(return_value=True)
        s.pointer = Mock(side_effect=lambda obj, cls, name: {'Player':3, 'Pawn':2, 'Controller':1}[name])
        s.field = Mock(return_value={'type':'ByteProperty', 'size':1, 'address':200})
        s.game = Mock()
        s.game.read.return_value = bytes([role])
        return s

    def test_offline_or_host_authority_and_guest_are_allowed(self):
        for role in (2,3):
            with self.subTest(role=role):
                scope=LocalPlayerScope(self.session(role))
                self.assertEqual(scope.validate(), role)
                scope.session.game.write.assert_not_called()

    def test_remote_unpossessed_and_unknown_roles_are_refused(self):
        for role in (0,1,4,255):
            with self.subTest(role=role), self.assertRaises(RuntimeError):
                LocalPlayerScope(self.session(role))

    def test_connection_owner_is_not_a_local_player(self):
        s=self.session();s.is_a.return_value=False
        with self.assertRaises(RuntimeError):LocalPlayerScope(s)

    def test_recycled_controller_pawn_or_local_player_is_refused(self):
        for changed in (1,2,3):
            s=self.session();scope=LocalPlayerScope(s)
            s.identity.side_effect=lambda p: 'recycled' if p==changed else s.identities[p]
            with self.subTest(changed=changed),self.assertRaises(RuntimeError):scope.validate()

    def test_possession_and_player_replacement_are_refused(self):
        for name in ('Player','Pawn','Controller'):
            s=self.session();scope=LocalPlayerScope(s)
            s.pointer.side_effect=lambda obj, cls, prop: 99 if prop==name else {'Player':3,'Pawn':2,'Controller':1}[prop]
            with self.subTest(name=name),self.assertRaises(RuntimeError):scope.validate()

    def test_failed_scope_blocks_camera_update_before_any_write(self):
        s=CameraSession.__new__(CameraSession)
        s.local_scope=Mock();s.local_scope.validate.side_effect=RuntimeError('remote pawn')
        s.game=Mock()
        with self.assertRaises(RuntimeError):s.update()
        s.game.write.assert_not_called()
