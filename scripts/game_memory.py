"""Read-only access to the fingerprinted local game for camera compatibility research.

No debugger attachment, remote threads, executable patches, or memory writes.
"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import struct

from build_info import GAME_EXE, GAME_SHA256, GAME_BUILD


# Discovery names are not a compatibility allowlist: every image must still
# pass the pinned executable-identity check. The GDK name lets another store
# build produces an explicit compatibility result rather than an endless wait.
GAME_PROCESS_NAMES = (GAME_EXE, 'Dungeons-WinGDK-Shipping.exe')
GAME_EXE_PATHS = (
    Path('Dungeons') / 'Binaries' / 'Win64' / GAME_EXE,
    Path('Dungeons') / 'Binaries' / 'WinGDK' / GAME_PROCESS_NAMES[1],
)
QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
MAX_PROCESS_ENTRIES = 65536


class GameCompatibilityError(RuntimeError):
    """A compatibility failure with a stable, path-free support reason code."""
    def __init__(self, message, reason_code, *, windows_error=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.winerror = windows_error


class _ProcessEntry(C.Structure):
    # PROCESSENTRY32W uses a pointer-sized ULONG_PTR even on 64-bit Windows.
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD),
                ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', C.c_size_t),
                ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
                ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', W.LONG),
                ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]


def memory_error(operation, address, length, transferred):
    """Keep the Windows error type/code and identify the failed memory request."""
    error = C.WinError(C.get_last_error() or 299)
    error.strerror += (f' ({operation} at 0x{address:X}, '
                      f'{transferred}/{length} bytes transferred)')
    return error


def validate_executable_path(exe, game=None):
    """Scope the actual image path; this alone never authorizes memory writes."""
    exe = Path(exe).resolve()
    if exe.name.casefold() not in {name.casefold() for name in GAME_PROCESS_NAMES}:
        raise GameCompatibilityError('Refusing a process other than Minecraft Dungeons II.',
                                     'DFP-WRONG-EXE')
    if game is not None and exe not in {(Path(game) / relative).resolve() for relative in GAME_EXE_PATHS}:
        raise GameCompatibilityError('The running game is not in the selected installation.',
                                     'DFP-SELECTION-MISMATCH')
    return exe


def validate_executable(exe, game=None):
    """Validate the actual running image, wherever the player installed it."""
    exe = validate_executable_path(exe, game)
    from executable_identity import identify_executable
    try:
        identity = identify_executable(exe, expected_sha256=GAME_SHA256)
    except OSError as error:
        reason = ('DFP-FILE-ACCESS' if isinstance(error, PermissionError) or getattr(error, 'winerror', None) == 5
                  else 'DFP-FILE-MISSING' if isinstance(error, FileNotFoundError) else 'DFP-CHECK-INCOMPLETE')
        raise GameCompatibilityError(str(error), reason, windows_error=getattr(error, 'winerror', None)) from error
    except ValueError as error:
        raise GameCompatibilityError(str(error), 'DFP-CHECK-INCOMPLETE') from error
    if not identity['compatible']:
        raise GameCompatibilityError(f'Unsupported game version. This mod supports {GAME_BUILD}. '
                                     'No game data was changed. A mod update is needed for this executable.',
                                     'DFP-UNTESTED-BUILD')
    return exe


def _query_kernel():
    kernel = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'GetExitCodeProcess': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
        'GetProcessTimes': ([W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        'CreateToolhelp32Snapshot': ([W.DWORD, W.DWORD], W.HANDLE),
        'Process32FirstW': ([W.HANDLE, C.POINTER(_ProcessEntry)], W.BOOL),
        'Process32NextW': ([W.HANDLE, C.POINTER(_ProcessEntry)], W.BOOL),
        'CloseHandle': ([W.HANDLE], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(kernel, name).argtypes = args
        getattr(kernel, name).restype = result
    return kernel


def _process_entries(kernel):
    """Read a bounded Unicode process snapshot without a launcher or shell.

    tasklist output depends on the Windows console code page. Decoding an
    unrelated application's name could otherwise prevent game discovery on
    another language's Windows installation. Toolhelp exposes Unicode names
    directly and needs no handles to unrelated processes.
    """
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.dwSize = C.sizeof(entry)
        rows = []
        available = kernel.Process32FirstW(snapshot, C.byref(entry))
        while available:
            if len(rows) >= MAX_PROCESS_ENTRIES:
                raise RuntimeError('Windows returned too many process entries. Check again.')
            rows.append((entry.szExeFile, entry.th32ProcessID))
            available = kernel.Process32NextW(snapshot, C.byref(entry))
        error = C.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise C.WinError(error)
        return rows
    finally:
        kernel.CloseHandle(snapshot)


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

    Entries contain pid, name, executable, creation_time, error, reason_code
    and windows_error. A candidate
    whose identity cannot be read remains in the results with an error, so the
    caller cannot mistake an inaccessible running game for an absent one.
    No executable hashing, VM access, registry lookup or launcher is required.
    """
    kernel = _query_kernel()
    names = {name.casefold() for name in GAME_PROCESS_NAMES}
    rows = [row for row in _process_entries(kernel) if row[0].casefold() in names]
    # Windows can keep a terminated process listed while another process
    # still holds a handle. Do not reconnect to that dead process repeatedly.
    live, seen = [], set()
    for row in rows:
        if not 0 < row[1] <= 0xFFFFFFFF:
            raise RuntimeError('Windows returned an invalid game process identifier.')
        pid = row[1]
        if pid in seen:
            continue
        seen.add(pid)
        candidate = dict(pid=pid, name=row[0], executable=None, creation_time=None, error=None,
                         reason_code=None, windows_error=None)
        handle = kernel.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = C.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: this PID vanished after the snapshot.
                continue
            candidate['error'] = f'Cannot inspect the running game process (Windows error {error}).'
            candidate['reason_code'] = 'DFP-PROCESS-ACCESS' if error == 5 else 'DFP-PROCESS-IDENTITY'
            candidate['windows_error'] = error
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
                raise GameCompatibilityError('The game process changed during discovery. Check again.',
                                             'DFP-PROCESS-CHANGED')
            if not kernel.GetExitCodeProcess(handle, C.byref(code)):
                raise C.WinError(C.get_last_error())
            if code.value != STILL_ACTIVE:
                continue
            candidate.update(executable=str(executable), creation_time=created)
        except (OSError, RuntimeError) as error:
            candidate['error'] = str(error)
            candidate['windows_error'] = getattr(error, 'winerror', None)
            candidate['reason_code'] = getattr(error, 'reason_code',
                'DFP-PROCESS-ACCESS' if candidate['windows_error'] == 5 else 'DFP-PROCESS-IDENTITY')
        finally:
            kernel.CloseHandle(handle)
        live.append(candidate)
    return sorted(live, key=lambda item: item['pid'])


def find_game_pid():
    try:
        candidates = discover_game_processes()
    except (OSError, RuntimeError) as error:
        if isinstance(error, GameCompatibilityError):
            raise
        raise GameCompatibilityError(str(error), 'DFP-DISCOVERY-FAILED',
                                     windows_error=getattr(error, 'winerror', None)) from error
    if len(candidates) > 1:
        raise GameCompatibilityError('Close duplicate copies of the game before using the camera helper.',
                                     'DFP-MULTIPLE-GAMES')
    if candidates and candidates[0]['error']:
        raise GameCompatibilityError('The game is running, but its executable could not be inspected. '
                                     'Close duplicate copies and run the game and helper as the same Windows user. '
                                     + candidates[0]['error'],
                                     candidates[0].get('reason_code') or 'DFP-PROCESS-IDENTITY',
                                     windows_error=candidates[0].get('windows_error'))
    return candidates[0]['pid'] if candidates else None


class GameReader:
    ACCESS = 0x0410  # QUERY_INFORMATION | VM_READ only.

    def __init__(self, pid=None, game=None):
        if C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Use 64-bit Python.")
        if pid is None:
            pid = find_game_pid()
            if pid is None:
                raise GameCompatibilityError('Start the game first.', 'DFP-GAME-NOT-FOUND')
        self.pid = pid
        self.kernel = _query_kernel()
        self.kernel.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        self.kernel.ReadProcessMemory.restype = W.BOOL
        self.handle = None
        self.build_profile = 'steam-25647713'
        self.mapped_identity = None
        # Even CameraMemory first opens a query-only handle. An unknown store
        # executable must be rejected before requesting VM_WRITE/VM_OPERATION.
        query_handle = self.kernel.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not query_handle:
            error = C.get_last_error()
            if error == 5:
                raise GameCompatibilityError('Windows denied access to the game process. '
                                             'Run the game and Helper as the same Windows user and privilege level.',
                                             'DFP-PROCESS-ACCESS', windows_error=error)
            raise C.WinError(error)
        try:
            identity = _process_identity(self.kernel, query_handle)
            gdk = identity[0].name.casefold() == 'dungeons-wingdk-shipping.exe'
            self.executable = (validate_executable_path(identity[0], game) if gdk
                               else validate_executable(identity[0], game))
            # A protected GDK file is identified from pinned mapped code/layout
            # using a READ-ONLY handle first. Never upgrade an unknown image.
            self.handle = self.kernel.OpenProcess(0x0410 if gdk else self.ACCESS, False, pid)
            if not self.handle:
                error = C.get_last_error()
                if error == 5:
                    raise GameCompatibilityError('Windows denied access to the validated game process. '
                                                 'Run the game and Helper as the same Windows user and privilege level.',
                                                 'DFP-PROCESS-ACCESS', windows_error=error)
                raise C.WinError(error)
            # Keep the original handle alive across reopening and verify the
            # executable path and process creation time on the usable handle.
            if _process_identity(self.kernel, self.handle) != identity:
                raise GameCompatibilityError('The game process changed during validation. Start the helper again.',
                                             'DFP-PROCESS-CHANGED')
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
            if gdk:
                from mapped_identity import identify_mapped_gdk
                try:
                    self.mapped_identity = identify_mapped_gdk(self)
                except ValueError as error:
                    raise GameCompatibilityError(str(error), 'DFP-UNTESTED-BUILD') from error
                self.build_profile = self.mapped_identity['profile']
                if _process_identity(self.kernel, self.handle) != identity:
                    raise GameCompatibilityError('The game changed during mapped validation.', 'DFP-PROCESS-CHANGED')
                if self.ACCESS != 0x0410:
                    readonly = self.handle
                    self.handle = None
                    try:
                        self.handle = self.kernel.OpenProcess(self.ACCESS, False, pid)
                        if not self.handle:
                            raise C.WinError(C.get_last_error())
                        if _process_identity(self.kernel, self.handle) != identity:
                            raise GameCompatibilityError('The game changed before attachment.', 'DFP-PROCESS-CHANGED')
                        code = W.DWORD()
                        if not self.kernel.GetExitCodeProcess(self.handle, C.byref(code)):
                            raise C.WinError(C.get_last_error())
                        if code.value != STILL_ACTIVE:
                            raise GameCompatibilityError('The game closed before attachment.', 'DFP-PROCESS-CHANGED')
                    finally:
                        self.kernel.CloseHandle(readonly)
        except Exception as error:
            self.close()
            if isinstance(error, OSError) and getattr(error, 'winerror', None) == 5:
                raise GameCompatibilityError('Windows denied access while identifying the game. '
                                             'Run the game and Helper as the same Windows user and privilege level.',
                                             'DFP-PROCESS-ACCESS', windows_error=5) from error
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
