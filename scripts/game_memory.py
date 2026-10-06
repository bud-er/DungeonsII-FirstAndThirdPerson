"""Read-only access to the fingerprinted local game for camera compatibility research.

No debugger attachment, remote threads, executable patches, or memory writes.
"""
import csv
import ctypes as C
from ctypes import wintypes as W
import hashlib
from pathlib import Path
import struct
import subprocess

from build_info import GAME_EXE, GAME_SHA256, GAME_BUILD


# Discovery names are not a compatibility allowlist: every image must still
# match GAME_SHA256. The GDK name is recognized so that another Windows store
# build produces an explicit compatibility result rather than an endless wait.
GAME_PROCESS_NAMES = (GAME_EXE, 'Dungeons-WinGDK-Shipping.exe')
GAME_EXE_PATHS = (
    Path('Dungeons') / 'Binaries' / 'Win64' / GAME_EXE,
    Path('Dungeons') / 'Binaries' / 'WinGDK' / GAME_PROCESS_NAMES[1],
)
QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def memory_error(operation, address, length, transferred):
    """Keep the Windows error type/code and identify the failed memory request."""
    error = C.WinError(C.get_last_error() or 299)
    error.strerror += (f' ({operation} at 0x{address:X}, '
                      f'{transferred}/{length} bytes transferred)')
    return error


def validate_executable(exe, game=None):
    """Validate the actual running image, wherever the player installed it."""
    exe = Path(exe).resolve()
    if exe.name.casefold() not in {name.casefold() for name in GAME_PROCESS_NAMES}:
        raise RuntimeError('Refusing a process other than Minecraft Dungeons II.')
    if game is not None and exe not in {(Path(game) / relative).resolve() for relative in GAME_EXE_PATHS}:
        raise RuntimeError('The running game is not in the selected installation.')
    with exe.open('rb') as source:
        fingerprint = hashlib.file_digest(source, 'sha256').hexdigest()
    if fingerprint != GAME_SHA256:
        raise RuntimeError(f'Unsupported game version. This mod supports {GAME_BUILD}. '
                           'No game data was changed. A mod update is needed for this executable.')
    return exe


def _query_kernel():
    kernel = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'GetExitCodeProcess': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
        'GetProcessTimes': ([W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        'CloseHandle': ([W.HANDLE], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(kernel, name).argtypes = args
        getattr(kernel, name).restype = result
    return kernel


def _process_identity(kernel, handle):
    name, size = C.create_unicode_buffer(32768), W.DWORD(32768)
    if not kernel.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)):
        raise C.WinError(C.get_last_error())
    if not name.value or not Path(name.value).is_absolute():
        raise RuntimeError('The game returned an invalid executable path.')
    created, exited, system, user = (W.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(system), C.byref(user)):
        raise C.WinError(C.get_last_error())
    creation_time = (created.dwHighDateTime << 32) | created.dwLowDateTime
    if not creation_time:
        raise RuntimeError('The game process creation time is unavailable.')
    return Path(name.value).resolve(), creation_time


def discover_game_processes():
    """Return launch-independent candidates using query-only process handles.

    Entries contain pid, name, executable, creation_time and error. A candidate
    whose identity cannot be read remains in the results with an error, so the
    caller cannot mistake an inaccessible running game for an absent one.
    No executable hashing, VM access, registry lookup or launcher is required.
    """
    result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                            capture_output=True, text=True, check=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    names = {name.casefold() for name in GAME_PROCESS_NAMES}
    rows = [row for row in csv.reader(result.stdout.splitlines()) if row and row[0].casefold() in names]
    # Windows can keep a terminated process in tasklist while another process
    # still holds a handle. Do not reconnect to that dead process repeatedly.
    kernel = _query_kernel()
    live, seen = [], set()
    for row in rows:
        if len(row) < 2 or not row[1].isdigit() or not 0 < int(row[1]) <= 0xFFFFFFFF:
            raise RuntimeError('Windows returned an invalid game process identifier.')
        pid = int(row[1])
        if pid in seen:
            continue
        seen.add(pid)
        candidate = dict(pid=pid, name=row[0], executable=None, creation_time=None, error=None)
        handle = kernel.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = C.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: this PID vanished after tasklist.
                continue
            candidate['error'] = f'Cannot inspect the running game process (Windows error {error}).'
            live.append(candidate)
            continue
        try:
            code = W.DWORD()
            if not kernel.GetExitCodeProcess(handle, C.byref(code)):
                raise C.WinError(C.get_last_error())
            if code.value != STILL_ACTIVE:
                continue
            executable, created = _process_identity(kernel, handle)
            if executable.name.casefold() != row[0].casefold():
                raise RuntimeError('The game process changed during discovery. Check again.')
            if not kernel.GetExitCodeProcess(handle, C.byref(code)):
                raise C.WinError(C.get_last_error())
            if code.value != STILL_ACTIVE:
                continue
            candidate.update(executable=str(executable), creation_time=created)
        except (OSError, RuntimeError) as error:
            candidate['error'] = str(error)
        finally:
            kernel.CloseHandle(handle)
        live.append(candidate)
    return sorted(live, key=lambda item: item['pid'])


def find_game_pid():
    candidates = discover_game_processes()
    if len(candidates) > 1:
        raise RuntimeError('Close duplicate copies of the game before using the camera helper.')
    if candidates and candidates[0]['error']:
        raise RuntimeError('The game is running, but its executable could not be inspected. '
                           'Close duplicate copies and run the game and helper as the same Windows user. '
                           + candidates[0]['error'])
    return candidates[0]['pid'] if candidates else None


class GameReader:
    ACCESS = 0x0410  # QUERY_INFORMATION | VM_READ only.

    def __init__(self, pid=None, game=None):
        if C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Use 64-bit Python.")
        if pid is None:
            pid = find_game_pid()
            if pid is None:
                raise RuntimeError('Start the game first.')
        self.pid = pid
        self.kernel = _query_kernel()
        self.kernel.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        self.kernel.ReadProcessMemory.restype = W.BOOL
        self.handle = None
        # Even CameraMemory first opens a query-only handle. An unknown store
        # executable must be rejected before requesting VM_WRITE/VM_OPERATION.
        query_handle = self.kernel.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not query_handle:
            raise C.WinError(C.get_last_error())
        try:
            identity = _process_identity(self.kernel, query_handle)
            self.executable = validate_executable(identity[0], game)
            self.handle = self.kernel.OpenProcess(self.ACCESS, False, pid)
            if not self.handle:
                raise C.WinError(C.get_last_error())
            # Keep the original handle alive across reopening and verify the
            # executable path and process creation time on the usable handle.
            if _process_identity(self.kernel, self.handle) != identity:
                raise RuntimeError('The game process changed during validation. Start the helper again.')
            code = W.DWORD()
            if not self.kernel.GetExitCodeProcess(self.handle, C.byref(code)):
                raise C.WinError(C.get_last_error())
            if code.value != STILL_ACTIVE:
                raise C.WinError(1168)  # The validated process has ended.
            psapi = C.WinDLL("psapi", use_last_error=True)
            psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, C.POINTER(W.HMODULE), W.DWORD, C.POINTER(W.DWORD), W.DWORD]
            psapi.EnumProcessModulesEx.restype = W.BOOL
            modules, needed = (W.HMODULE * 2048)(), W.DWORD()
            if not psapi.EnumProcessModulesEx(self.handle, modules, C.sizeof(modules), C.byref(needed), 3) or not needed.value:
                raise C.WinError(C.get_last_error())
            self.base = modules[0]
            if self.read(self.base, 2) != b"MZ":
                raise RuntimeError("Main executable mapping not found.")
        except Exception:
            self.close()
            raise
        finally:
            self.kernel.CloseHandle(query_handle)

    def read(self, address, length):
        if not 0 < length <= 16 * 1024 * 1024 or not 0x10000 <= address < 0x7FFFFFFFFFFF:
            raise ValueError("Read address or length outside the diagnostic limits.")
        buffer, count = C.create_string_buffer(length), C.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, length, C.byref(count)) or count.value != length:
            raise memory_error('read', address, length, count.value)
        return buffer.raw

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
