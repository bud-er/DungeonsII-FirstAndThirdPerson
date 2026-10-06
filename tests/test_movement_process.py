"""Exercise real spawn/mailbox lifecycle without opening the game."""
import pickle
import struct
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from movement_pulse import MovementProcess


def mailbox_peer(mailbox, stop, stats, errors):
    stats[0]=1.
    try:
        while not stop.is_set():
            with mailbox.get_lock():
                raw=mailbox.get_obj()
                revision,length=struct.unpack('<QI',bytes(raw[:12]))
                message=pickle.loads(bytes(raw[12:12+length]))
            if message['yaw']==123.:
                errors.send('simulated ownership failure')
                return
            time.sleep(.005)  # In-flight native work completes before acknowledging.
            stats[4]=float(message['deadline']>time.monotonic())
            stats[3]=revision
            stats[1]+=1
    finally:
        stats[4]=stats[0]=0.
        errors.close()


class TestProcess(MovementProcess):
    def _start(self):
        self.stopping.clear();self.stats[:]=[0.]*5
        self.errors,sender=self.context.Pipe(duplex=False)
        self.process=self.context.Process(target=mailbox_peer,
            args=(self.mailbox,self.stopping,self.stats,sender),daemon=True)
        self.process.start();sender.close()


class ProcessTests(unittest.TestCase):
    def ready(self,p):
        deadline=time.monotonic()+5
        while not p.stats[0] and time.monotonic()<deadline:time.sleep(.001)
        self.assertTrue(p.stats[0])

    def test_latest_value_slot_does_not_queue_or_wait_for_slow_worker(self):
        p=TestProcess(None)
        try:
            p.renew(0.,());self.ready(p)
            start=time.monotonic()
            for i in range(500):p.renew(float(i%120),())
            self.assertLess(time.monotonic()-start,1.)
            with p.mailbox.get_lock():
                raw=p.mailbox.get_obj();revision,length=struct.unpack('<QI',bytes(raw[:12]))
                message=pickle.loads(bytes(raw[12:12+length]))
            self.assertEqual(revision,p.revision)
            self.assertEqual(message['yaw'],499%120)
            p.pause()
            self.assertGreaterEqual(p.stats[3],p.revision)
            self.assertEqual(p.stats[4],0.)
        finally:p.stop()
        self.assertIsNone(p.process)
        p.renew(0.,())
        try:self.ready(p)
        finally:p.stop()

    def test_parent_receives_worker_error_and_stop_closes_failed_child(self):
        p=TestProcess(None)
        p.renew(123.,())
        self.assertTrue(p.errors.poll(5))
        with self.assertRaisesRegex(RuntimeError,'ownership failure'):p.renew(0.,())
        with self.assertRaisesRegex(RuntimeError,'ownership failure'):p.stop()
        self.assertIsNone(p.process);self.assertIsNone(p.errors)

    def test_stop_before_setup_completes_never_leaves_a_child_running(self):
        p=TestProcess(None);p.renew(0.,());p.stop()
        self.assertIsNone(p.process)
        self.assertEqual(p.stats[4],0.)


if __name__=='__main__':unittest.main()
