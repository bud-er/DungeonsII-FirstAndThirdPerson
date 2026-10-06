"""Prepare read-only player discovery while the native camera is active."""
import threading


class CameraDiscovery:
    def __init__(self):
        self.thread = None
        self.result = self.error = None

    def start(self, factory):
        if self.thread is not None:
            return
        def prepare():
            try:
                self.result = factory()
            except Exception as error:
                self.error = error
        self.thread = threading.Thread(target=prepare, name='Camera discovery', daemon=True)
        self.thread.start()

    def poll(self):
        if self.thread is None or self.thread.is_alive():
            return None, None
        self.thread.join()
        result, error = self.result, self.error
        self.thread = self.result = self.error = None
        return result, error

    def close(self):
        # The reader's process handle stays open until read-only work finishes.
        if self.thread is not None:
            self.thread.join()
            self.thread = self.result = self.error = None
