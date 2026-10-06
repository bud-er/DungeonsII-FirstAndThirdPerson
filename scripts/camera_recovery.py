"""Bound camera recovery attempts while retaining restoration/ownership guards."""
from pathlib import Path
import traceback


def failure_path(error):
    """Keep a short actionable call path without exposing installation paths."""
    frames = traceback.extract_tb(error.__traceback__)
    return ' -> '.join(f'{Path(frame.filename).name}:{frame.lineno} {frame.name}'
                       for frame in frames[-6:])


class CameraRecovery:
    delays = (.5, 1., 2.)
    stable_seconds = 10.

    def __init__(self):
        self.reset()

    def reset(self):
        self.attempts = 0
        self.retry_at = 0.
        self.healthy_since = None
        self.fresh = False

    def failed(self, now):
        self.healthy_since = None
        self.fresh = True
        self.attempts += 1
        if self.attempts > len(self.delays):
            return False
        self.retry_at = now + self.delays[self.attempts-1]
        return True

    def healthy(self, now):
        self.fresh = False
        if self.healthy_since is None:
            self.healthy_since = now
        if now-self.healthy_since >= self.stable_seconds:
            self.attempts = 0
