from pathlib import Path
import math
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from keyboard_movement import KeyboardMovement, DisabledKey, keyboard_axes, controller_axes, movement_vector, ZERO
from external_camera import NativeInput


class Memory:
    def __init__(self):
        self.data, self.writes, self.fail = {}, [], False
    def put(self, address, value):
        self.data.update({address+i:v for i,v in enumerate(value)})
    def read(self, address, size):
        return bytes(self.data.get(address+i, 0) for i in range(size))
    def write(self, address, value):
        if self.fail:
            raise OSError('write failed')
        self.writes.append((address, value));self.put(address, value)
    def u64(self, address):
        return struct.unpack('<Q', self.read(address,8))[0]


class KeyboardTests(unittest.TestCase):
    def profile(self):
        p = KeyboardMovement.__new__(KeyboardMovement)
        g = Memory()
        ids = {100:1, 200:2, 300:3, 600:6}
        s = SimpleNamespace(pc=100, pawn=200, game=g, identities=ids.copy(), identity=lambda o:ids[o], yaw=0.)
        pointers = {(100,'Pawn'):200, (200,'Controller'):100, (200,'CharacterMovement'):300}
        s.pointer = lambda obj, cls, name:pointers[(obj,name)]
        p.session,p.game,p.movement,p.address = s,g,300,400
        p.axes,p.eligible,p.pending = (0.,0.),True,None
        p.bindings,p.applied,p.restore_failures,p.next_collect = {},False,set(),math.inf
        p.collect = lambda:None
        return p,g,ids,pointers

    def binding(self, p, g):
        g.put(700, struct.pack('<Qii', 800, 1, 1))
        row = bytearray(0x50)
        row[0x20:0x28] = struct.pack('<Q', 999)
        row[0x28:0x30] = struct.pack('<II', 83, 0)
        row[0x30:0x40] = bytes(range(16))
        g.put(800, row)
        r = DisabledKey(p, 600, 700, g.read(700,16), 800, bytes(row[0x28:0x30]))
        p.bindings[1] = r
        return r

    def test_opposing_keys_cancel_and_diagonal_is_normalized(self):
        self.assertEqual(keyboard_axes(1,1,1,1),(0.,0.))
        self.assertEqual(keyboard_axes(0,0,1,0),(0.,-1.))
        self.assertAlmostEqual(math.hypot(*keyboard_axes(1,0,0,1)),1.)

    def test_forward_backward_and_strafe_rotate_with_camera_yaw(self):
        def vector(yaw, axes):return struct.unpack('<ddd',movement_vector(yaw,axes))
        self.assertEqual(vector(0,(0,1)),(1.,0.,0.))
        self.assertEqual(vector(0,(0,-1)),(-1.,0.,0.))
        self.assertAlmostEqual(vector(90,(0,1))[1],1.)
        self.assertAlmostEqual(vector(90,(1,0))[0],-1.)

    def test_controller_keeps_analog_strength_and_clamps_full_diagonals(self):
        self.assertEqual(controller_axes((.3,.4)),(.3,.4))
        self.assertEqual(controller_axes((0.,-1.)),(0.,-1.))
        self.assertAlmostEqual(math.hypot(*controller_axes((1.,-1.))),1.)
        for axes in ((float('nan'),0),(0,2)):
            with self.assertRaises(RuntimeError):controller_axes(axes)

    def test_controller_input_uses_pending_movement_without_changing_aim_or_transform(self):
        p,g,_,_=self.profile();p.session.yaw=90.;p.set_input(controller_axes((0.,-1.)));p.update()
        self.assertEqual([a for a,_ in g.writes],[400])
        self.assertAlmostEqual(struct.unpack('<ddd',g.read(400,24))[1],-1.)

    def test_invalid_nonfinite_or_oversized_input_rejected(self):
        for yaw, axes in ((float('nan'),(0,1)),(0,(2,0)),(0,(0,float('inf')))):
            with self.assertRaises(RuntimeError):movement_vector(yaw,axes)

    def test_native_pending_input_is_written_and_release_clears_only_our_vector(self):
        p,g,_,_=self.profile();p.set_input((0.,1.));p.update()
        self.assertEqual(g.read(400,24),movement_vector(0,(0,1)))
        p.set_input((0.,0.));p.update()
        self.assertEqual(g.read(400,24),ZERO)

    def test_game_consumed_or_controller_replaced_input_is_not_overwritten_on_release(self):
        for replacement in (ZERO,movement_vector(180,(0,1))):
            p,g,_,_=self.profile();p.set_input((0.,1.));p.update()
            g.put(400,replacement);g.writes.clear();p.restore()
            self.assertFalse(g.writes)
            self.assertEqual(g.read(400,24),replacement)

    def test_focus_loss_and_native_move_lock_stop_keyboard_input(self):
        for focus, lock in ((False,0),(True,1)):
            p,g,_,_=self.profile();p.set_input((0.,1.));p.update()
            g.put(100+0x339,bytes([lock]));p.set_input((0.,1.),eligible=focus);p.update()
            self.assertEqual(g.read(400,24),ZERO)

    def test_player_recycling_and_ownership_change_prevent_writes(self):
        for recycle in (True,False):
            p,g,ids,pointers=self.profile();p.set_input((0.,1.))
            if recycle:ids[200]=9
            else:pointers[(200,'Controller')]=999
            with self.assertRaises(RuntimeError):p.update()
            self.assertFalse(g.writes)

    def test_key_disabling_preserves_cached_pointer_ownership_and_restores_fname(self):
        p,g,_,_=self.profile();r=self.binding(p,g);original=g.read(800,0x50)
        r.disable()
        self.assertEqual(g.read(800+0x28,8),bytes(8))
        self.assertEqual(g.read(800+0x30,16),original[0x30:0x40])
        g.put(800+0x40,b'\x02')  # Engine-owned trigger bookkeeping.
        r.restore()
        self.assertEqual(g.read(800+0x28,24),original[0x28:0x40])
        self.assertEqual(g.read(800+0x40,1),b'\x02')

    def test_retired_array_changed_row_or_owner_serial_never_written(self):
        for mode in ('array','row','owner','key'):
            p,g,ids,_=self.profile();r=self.binding(p,g);r.disable()
            if mode=='array':g.put(700,struct.pack('<Qii',900,1,1))
            if mode=='row':g.put(800+0x20,struct.pack('<Q',123))
            if mode=='owner':ids[600]=99
            if mode=='key':g.put(800+0x28,struct.pack('<Q',44))
            g.writes.clear();self.assertTrue(r.restore());self.assertFalse(r.disable());self.assertFalse(g.writes)

    def test_failed_restore_retains_binding_for_retry(self):
        p,g,_,_=self.profile();r=self.binding(p,g);p.update();g.fail=True
        p.restore();self.assertTrue(p.restore_failures);self.assertTrue(r.applied)
        g.fail=False;p.restore();self.assertFalse(p.restore_failures);self.assertFalse(r.applied)

    def test_native_input_does_not_sample_keyboard_without_focus_or_with_active_controller(self):
        c=NativeInput.__new__(NativeInput)
        c.controller_activity=True;c.using_gamepad=True
        c.user=SimpleNamespace(GetAsyncKeyState=lambda _:self.fail('should not sample keys'))
        self.assertEqual(c.keyboard(123),(0.,0.))
        c.controller_activity=False
        self.assertEqual(c.keyboard(None),(0.,0.))
        c.user.GetAsyncKeyState=lambda key:0x8000 if key in (0x57,0x44) else 0
        self.assertAlmostEqual(math.hypot(*c.keyboard(123)),1.)
        self.assertFalse(c.using_gamepad)

    def test_inherited_disabled_binding_is_recaptured_from_source_key(self):
        p,g,_,_=self.profile();r=self.binding(p,g);r.disable()
        p.source_keys={(999,bytes(range(16))):r.original}
        p.session.field=lambda *_:{'address':700}
        p.session.reflection=SimpleNamespace(header=lambda _:dict(name='IA_OpenUI_MultiplayerWheel'),
                                             name=lambda index,number:'None' if index==0 else 'S')
        p.bindings={}
        p.capture(600,'EnhancedPlayerInput','EnhancedActionMappings')
        inherited=next(iter(p.bindings.values()))
        self.assertTrue(inherited.applied)
        inherited.restore()  # Rebuild discovered during restore, before update.
        self.assertEqual(g.read(800+0x28,8),r.original)

    def test_runtime_cache_is_remembered_when_source_and_runtime_caches_differ(self):
        p,g,_,_=self.profile();r=self.binding(p,g)
        p.source_keys={(999,bytes(16)):r.original};p.bindings={}
        p.session.field=lambda *_:{'address':700}
        p.session.reflection=SimpleNamespace(header=lambda _:dict(name='IA_OpenUI_MultiplayerWheel'),
                                             name=lambda index,number:'None' if index==0 else 'S')
        p.capture(600,'EnhancedPlayerInput','EnhancedActionMappings')
        self.assertEqual(p.source_keys[(999,bytes(range(16)))],r.original)
        next(iter(p.bindings.values())).disable();p.bindings={}
        p.capture(600,'EnhancedPlayerInput','EnhancedActionMappings')
        next(iter(p.bindings.values())).restore()
        self.assertEqual(g.read(800+0x28,8),r.original)

    def test_keyboard_rebuild_is_disabled_before_the_periodic_collection_deadline(self):
        p,g,_,_=self.profile();r=self.binding(p,g);r.disable()
        replacement=g.read(800,0x50)
        g.put(900,replacement);g.put(900+0x28,r.original)
        g.put(700,struct.pack('<Qii',900,1,1))
        p.source_keys={};p.session.field=lambda *_:{'address':700}
        p.session.reflection=SimpleNamespace(header=lambda _:dict(name='IA_OpenUI_MultiplayerWheel'),
                                             name=lambda index,number:'None' if index==0 else 'S')
        p.collect=lambda:p.capture(600,'EnhancedPlayerInput','EnhancedActionMappings')
        p.update()
        self.assertEqual(g.read(900+0x28,8),bytes(8))
        self.assertEqual(len(p.bindings),1)
        p.restore();self.assertEqual(g.read(900+0x28,8),r.original)

    def test_keyboard_dodge_sampling_keeps_controller_priority_and_tracks_the_held_key(self):
        from roll_direction import KEYBOARD_DODGE
        c=NativeInput.__new__(NativeInput);c.controller_activity=False
        c.user=SimpleNamespace(GetAsyncKeyState=lambda k:0x8000 if k==0x52 else 0)
        self.assertEqual(c.roll_buttons(0),KEYBOARD_DODGE)
        self.assertEqual(c.roll_buttons(0x80),KEYBOARD_DODGE|0x80)
        c.controller_activity=True
        c.user.GetAsyncKeyState=lambda _:self.fail('Controller input must not poll keyboard dodge')
        self.assertEqual(c.roll_buttons(0x80),0x80)


if __name__=='__main__':unittest.main()
