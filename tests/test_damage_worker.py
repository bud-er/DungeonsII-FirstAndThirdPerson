import multiprocessing as mp
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from damage_worker import DamageOverlayWorker


class StalledRenderer:
    def __init__(self, signals):
        self.started, self.release, self.closed = signals

    def draw(self, intensity):
        self.started.set()
        self.release.wait(10)

    def close(self):
        self.closed.set()


class BrokenRenderer:
    def __init__(self, unused):
        raise OSError('simulated display failure')


class DisplayIsolation(unittest.TestCase):
    def test_blocked_native_renderer_cannot_block_camera_updates(self):
        context = mp.get_context('spawn')
        started, release, closed = (context.Event() for _ in range(3))
        display = DamageOverlayWorker((started, release, closed), renderer_factory=StalledRenderer)
        failures, completed = [], threading.Event()
        try:
            self.assertTrue(started.wait(5), 'Display worker did not start')
            def camera_ticks():
                try:
                    for i in range(1000):
                        display.draw((i % 100)/100)
                except Exception as error:
                    failures.append(error)
                finally:
                    completed.set()
            thread = threading.Thread(target=camera_ticks, daemon=True)
            thread.start()
            self.assertTrue(completed.wait(1), 'Camera updates waited for the renderer')
            self.assertFalse(release.is_set())
            self.assertFalse(failures)
            thread.join(1)
        finally:
            release.set()
            display.close()
        self.assertTrue(closed.is_set(), 'Renderer did not clean up its window')

    def test_worker_errors_reach_parent_without_a_blocking_wait(self):
        display = DamageOverlayWorker(0, renderer_factory=BrokenRenderer)
        try:
            self.assertTrue(display.errors.poll(5))
            with self.assertRaisesRegex(RuntimeError, 'simulated display failure'):
                display.draw(1)
        finally:
            display.close()
        display.close()  # Idempotent when an error and restoration both close it.


if __name__ == '__main__':
    unittest.main()
