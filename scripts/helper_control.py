"""A named Windows event requests restoration and shutdown without killing a helper."""
import ctypes as C
from ctypes import wintypes as W


class StopSignal:
    def __init__(self, name):
        if not name.startswith('Local\\DungeonsFirstPerson-Stop-'):
            raise ValueError('Invalid stop event name')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        for method, args, result in (
            ('CreateEventW', [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            ('SetEvent', [W.HANDLE], W.BOOL),
            ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
            ('CloseHandle', [W.HANDLE], W.BOOL),
        ):
            fn = getattr(self.kernel, method)
            fn.argtypes, fn.restype = args, result
        self.handle = self.kernel.CreateEventW(None, True, False, name)
        if not self.handle:
            raise C.WinError(C.get_last_error())

    def is_set(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())
        return result == 0

    def set(self):
        if not self.kernel.SetEvent(self.handle):
            raise C.WinError(C.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
