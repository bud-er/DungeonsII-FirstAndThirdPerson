import math
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from movement_pulse import MovementPulse, deadzoned_axis, PhysicalMovement
from keyboard_movement import movement_vector


class PulseTests(unittest.TestCase):
    def make(self):
        self.now = 10.
        self.value = bytes(24)
        self.writes = []
        self.locked = False
        self.focused_axes = (0., 1.)
        def read(address, size):
            if address == 400: return self.value
            if size == 1: return bytes([int(self.locked)])
            return bytes(size)
        def write(address, value):
            self.assertEqual(address, 400)
            self.value = value
            self.writes.append(value)
        s = SimpleNamespace(pc=100, pawn=200, camera=500, root=600, input_paused=False,
            local_scope=SimpleNamespace(validate=Mock()),
            pointer=lambda obj,cls,name:500 if name=='CurrentCamera' else 600)
        p = SimpleNamespace(session=s, game=SimpleNamespace(read=read, write=write),
                            address=400, validate=Mock())
        pulse = MovementPulse(p, SimpleNamespace(sample=lambda:self.focused_axes), clock=lambda:self.now)
        pulse.yaw, pulse.deadline = 90., self.now + pulse.LEASE
        return pulse, p, s

    def test_consumed_input_is_replenished_between_slow_main_frames(self):
        pulse,p,s = self.make()
        for _ in range(20):
            pulse.step()
            self.assertAlmostEqual(struct.unpack('<ddd',self.value)[1],1.)
            self.value=bytes(24)  # Native tick consumes input before next camera scan.
            self.now += .002
        self.assertEqual(len(self.writes),20)
        self.assertEqual(p.validate.call_count,20)
        self.assertEqual(s.local_scope.validate.call_count,20)

    def test_release_and_focus_loss_read_fresh_input_without_waiting_for_main_loop(self):
        for axes in (None,(0.,0.)):
            pulse,_,_=self.make();pulse.step();self.focused_axes=axes;pulse.step()
            self.assertEqual(self.value,bytes(24))
            self.assertIsNone(pulse.pending)

    def test_lease_expiry_and_backwards_clock_stop_stale_input(self):
        for delta in (.076,-.001):
            pulse,_,_=self.make();pulse.step();self.now+=delta;pulse.step()
            self.assertEqual(self.value,bytes(24));self.assertEqual(pulse.deadline,0.)
            self.assertEqual(pulse.expirations,1)

    def test_native_move_lock_menu_and_camera_change_block_pulses(self):
        for mode in ('lock','menu','camera','attach'):
            pulse,_,s=self.make();pulse.step()
            if mode=='lock':self.locked=True
            if mode=='menu':s.input_paused=True
            if mode=='camera':s.pointer=lambda *_:999
            if mode=='attach':s.pointer=lambda obj,cls,name:500 if name=='CurrentCamera' else 999
            pulse.step();self.assertEqual(self.value,bytes(24))

    def test_restored_or_rebuilt_binding_revokes_lease(self):
        for valid, enabled in ((False,False),(True,True)):
            pulse,p,_=self.make();pulse.step()
            pulse.records=(SimpleNamespace(valid=lambda:valid,row=700),)
            oldread=p.game.read
            p.game.read=lambda addr,n: b'X'*8 if enabled and addr==740 else oldread(addr,n)
            pulse.step();self.assertEqual(pulse.deadline,0.);self.assertEqual(self.value,bytes(24))

    def test_recycled_player_is_never_written(self):
        pulse,p,_=self.make();p.validate.side_effect=RuntimeError('retired')
        with self.assertRaises(RuntimeError):pulse.step()
        self.assertEqual(self.writes,[])

    def test_pause_does_not_overwrite_input_replaced_by_native_game(self):
        pulse,_,_=self.make();pulse.step()
        native=movement_vector(0.,(1.,0.));self.value=native
        self.writes.clear();pulse.pause()
        self.assertEqual(self.value,native);self.assertEqual(self.writes,[])

    def test_stop_joins_inflight_write_before_return_and_can_resume(self):
        pulse,p,_=self.make()
        entered,release=threading.Event(),threading.Event()
        original=p.game.write
        def delayed(addr,value):
            if value!=bytes(24):
                entered.set();release.wait(2)
            original(addr,value)
        p.game.write=delayed
        pulse.renew(0.,())
        self.assertTrue(entered.wait(2))
        stopped=threading.Event()
        thread=threading.Thread(target=lambda:(pulse.stop(),stopped.set()))
        thread.start()
        try:self.assertFalse(stopped.wait(.03))
        finally:release.set();thread.join(2)
        self.assertTrue(stopped.is_set());self.assertEqual(self.value,bytes(24))
        before=len(self.writes);time.sleep(.01);self.assertEqual(len(self.writes),before)
        pulse.renew(90.,())
        try:
            for _ in range(100):
                if len(self.writes)>before:break
                time.sleep(.001)
            self.assertGreater(len(self.writes),before)
        finally:pulse.stop()

    def test_worker_failure_is_reported_to_main_loop_and_no_retry_writes(self):
        pulse,p,_=self.make();p.validate.side_effect=RuntimeError('retired')
        pulse.renew(0.,())
        pulse.thread.join(2)
        with self.assertRaisesRegex(RuntimeError,'retired'):pulse.renew(0.,())
        self.assertEqual(self.writes,[])
        pulse.stop()

    def test_physical_deadzone_matches_camera_input(self):
        self.assertEqual(deadzoned_axis(0),0.)
        self.assertEqual(deadzoned_axis(5000),0.)
        self.assertEqual(deadzoned_axis(-32768),-1.)
        self.assertEqual(deadzoned_axis(32767),1.)
        self.assertTrue(0 < deadzoned_axis(16000) < 1)


if __name__=='__main__':unittest.main()
