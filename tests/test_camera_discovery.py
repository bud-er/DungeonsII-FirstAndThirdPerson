from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from camera_discovery import CameraDiscovery


class CameraDiscoveryTests(unittest.TestCase):
    def test_pending_discovery_never_exposes_partial_result_or_starts_twice(self):
        discovery=CameraDiscovery();entered,release=threading.Event(),threading.Event();calls=[]
        def factory():
            calls.append(1);entered.set();release.wait(2);return ('player','menus')
        discovery.start(factory);self.assertTrue(entered.wait(1))
        discovery.start(factory)
        self.assertEqual(discovery.poll(),(None,None))
        release.set();discovery.thread.join(1)
        self.assertEqual(discovery.poll(),(('player','menus'),None))
        self.assertEqual(calls,[1])
        self.assertEqual(discovery.poll(),(None,None))

    def test_error_is_delivered_and_retry_can_start(self):
        discovery=CameraDiscovery()
        def bad():raise RuntimeError('not playable')
        discovery.start(bad);discovery.thread.join(1)
        result,error=discovery.poll()
        self.assertIsNone(result);self.assertIsInstance(error,RuntimeError)
        discovery.start(lambda:'next player');discovery.thread.join(1)
        self.assertEqual(discovery.poll(),('next player',None))

    def test_close_finishes_reads_before_the_process_handle_can_close(self):
        discovery=CameraDiscovery();finished=threading.Event()
        discovery.start(lambda:finished.set());discovery.close()
        self.assertTrue(finished.is_set());self.assertIsNone(discovery.thread)


if __name__=='__main__':unittest.main()
