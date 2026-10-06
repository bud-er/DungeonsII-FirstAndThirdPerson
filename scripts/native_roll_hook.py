"""Opt-in, local-only roll direction selection before native prediction.

Three fixed-width call-site redirects run tiny leaf stubs on the game thread.
They retain the native R3 action, abilities, target data and roll physics.
There is no DLL loader, remote thread, analog-memory mirror or force edit.
This prototype requires the pinned executable and is not live-playtested.
"""
import contextlib
import ctypes as C
from ctypes import wintypes as W
import struct
import time

from inspect_camera import OBJECT_ARRAY_RVA
from build_info import NATIVE_ROLL_AVAILABLE

BOOL_GETTER = 0x57D0F70
PATCHES = {
    0x5AAD599: bytes.fromhex('e8d239d2ff'),
    0x5AAD752: bytes.fromhex('e81938d2ff'),
    0x5A997FB: bytes.fromhex('ba01000000'),
}
GUARDS = {
    0x5AAD59E: bytes.fromhex('84c074114d8bc6'),
    0x5AAD757: bytes.fromhex('84c074114d8bc6'),
    0x5A997F8: bytes.fromhex('488bcf'),
    0x5A99800: bytes.fromhex('e82b5570fe'),
    0x5AAD545: bytes.fromhex('4c8bb7d8020000'),
}
PAGE = 0x1000
CONFIG_SIZE = 96


def write_prefixes(before, after):
    """Possible readbacks of a complete or interrupted fixed-width write."""
    return {after[:size]+before[size:] for size in range(len(after)+1)}


def relative_branch(opcode, address, target):
    distance = target-(address+5)
    if not -(1 << 31) <= distance < 1 << 31:
        raise RuntimeError('Native roll branch is outside rel32 range')
    return bytes([opcode])+struct.pack('<i', distance)


class Assembler:
    """Only the small, fixed x64 instruction subset used by these leaf stubs."""
    def __init__(self, address):
        self.address, self.data, self.labels, self.fixups = address, bytearray(), {}, []

    def emit(self, data):
        self.data.extend(bytes.fromhex(data) if isinstance(data, str) else data)

    def label(self, name):
        if name in self.labels:
            raise RuntimeError('Duplicate stub label')
        self.labels[name] = len(self.data)

    def conditional(self, condition, label):
        self.emit(bytes([0x0F, condition])+bytes(4))
        self.fixups.append((len(self.data)-4, label))

    def branch(self, opcode, target):
        self.emit(relative_branch(opcode, self.address+len(self.data), target))

    def finish(self):
        for offset, label in self.fixups:
            struct.pack_into('<i', self.data, offset, self.labels[label]-(offset+4))
        return bytes(self.data)


def _cmp_config(a, register, offset):
    a.emit(bytes([0x48 | (4 if register >= 8 else 0), 0x3B,
                  0x40 | ((register & 7) << 3), offset]))


def _read_r10(a, register, offset, wide=True):
    a.emit(bytes([(0x4C if wide else 0x44) | (1 if register >= 8 else 0),
                  0x8B, 0x90 | (register & 7)])+struct.pack('<i', offset))


def _cmp_r10(a, register):
    a.emit(bytes([0x4C | (1 if register >= 8 else 0), 0x3B, 0xD0 | (register & 7)]))


def emit_gate(a, config, pc_register, pawn_register, offsets):
    # RAX/R10 and flags are saved by the caller. No XMM/nonvolatile register is
    # changed. Objects are dereferenced only after both live arguments match.
    a.emit(b'\x48\xb8'+struct.pack('<Q', config))
    a.emit('833801')  # cmp dword [rax],1
    a.conditional(0x85, 'foreign')
    _cmp_config(a, pc_register, 8)
    a.conditional(0x85, 'foreign')
    _cmp_config(a, pawn_register, 16)
    a.conditional(0x85, 'foreign')
    for register, index_offset, serial_address, serial_value in (
            (pc_register, 24, 32, 48), (pawn_register, 28, 40, 52)):
        _read_r10(a, register, 0xC, wide=False)
        a.emit(bytes.fromhex('443b50')+bytes([index_offset]))
        a.conditional(0x85, 'foreign')
        a.emit(bytes.fromhex('4c8b50')+bytes([serial_address]))
        a.emit('458b12')
        a.emit(bytes.fromhex('443b50')+bytes([serial_value]))
        a.conditional(0x85, 'foreign')
    _read_r10(a, pc_register, offsets['pc_pawn'])
    _cmp_r10(a, pawn_register)
    a.conditional(0x85, 'foreign')
    _read_r10(a, pawn_register, offsets['pawn_pc'])
    _cmp_r10(a, pc_register)
    a.conditional(0x85, 'foreign')
    _read_r10(a, pc_register, offsets['pc_player'])
    a.emit('4c3b5038')
    a.conditional(0x85, 'foreign')
    a.emit('458b520c443b505c')  # LocalPlayer index.
    a.conditional(0x85, 'foreign')
    a.emit('4c8b5050458b12443b5058')  # LocalPlayer serial.
    a.conditional(0x85, 'foreign')
    a.emit(bytes([0x44 | (1 if pawn_register >= 8 else 0), 0x0F, 0xB6,
                  0x90 | (pawn_register & 7)])+struct.pack('<i', offsets['role']))
    a.emit('4183fa02')
    a.conditional(0x82, 'foreign')
    a.emit('4183fa03')
    a.conditional(0x87, 'foreign')
    a.emit('833801')
    a.conditional(0x85, 'foreign')


def save_volatile(a):
    # Use the native caller's 32-byte shadow space, keeping RSP unchanged so
    # these call-free stubs can be unwound as leaf functions. LAHF/SETO retain
    # every arithmetic flag changed by CMP/INC, without PUSHF's stack change.
    a.emit('48894424084c895424109f0f90c04889442418')


def restore_volatile(a):
    # Saved AL is 0/1: adding 127 recreates OF; SAHF restores SF/ZF/AF/PF/CF.
    # Neither MOV nor the following RET/tail jump changes those flags.
    a.emit('488b442418047f9e4c8b542410488b442408')


def direction_stub(address, config, original_getter, offsets):
    a = Assembler(address)
    save_volatile(a)
    emit_gate(a, config, 14, 7, offsets)  # Activation's R14=PC, RDI=pawn.
    a.emit('f0ff4040c644240801')  # counter; saved AL=1.
    restore_volatile(a)
    a.emit('c3')
    a.label('foreign')
    a.emit('f0ff4048')
    restore_volatile(a)
    a.branch(0xE9, original_getter)  # Tail call with the original return address.
    return a.finish()


def stick_stub(address, config, offsets):
    a = Assembler(address)
    save_volatile(a)
    emit_gate(a, config, 7, 6, offsets)  # Getter's RDI=PC, RSI=pawn.
    a.emit('f0ff4044ba00000000')  # local enum 0.
    restore_volatile(a)
    a.emit('c3')
    a.label('foreign')
    a.emit('ba01000000')  # original enum 1, all other state preserved.
    restore_volatile(a)
    a.emit('c3')
    return a.finish()


def serial_address(session, obj):
    g = session.game
    index = g.u32(obj+0xC)
    array = g.base+OBJECT_ARRAY_RVA
    if index >= g.u32(array+0x24):
        raise RuntimeError('Native roll object index changed')
    chunk = g.u64(g.u64(array+0x10)+(index//65536)*8)
    item = chunk+(index % 65536)*24
    if g.u64(item) != obj:
        raise RuntimeError('Native roll object slot changed')
    return index, item+0x10, g.u32(item+0x10)


def scope_config(session):
    s = session
    s.local_scope.validate()
    pc = serial_address(s, s.pc)
    pawn = serial_address(s, s.pawn)
    player = serial_address(s, s.local_scope.player)
    raw = bytearray(CONFIG_SIZE)
    struct.pack_into('<QQIIQQIIQ', raw, 8, s.pc, s.pawn, pc[0], pawn[0],
                     pc[1], pawn[1], pc[2], pawn[2], s.local_scope.player)
    struct.pack_into('<QII', raw, 80, player[1], player[2], player[0])
    s.local_scope.validate()
    return bytes(raw)


def layout_offsets(s):
    result = {}
    for name, obj, cls, prop, kind, size in (
            ('pc_pawn', s.pc, 'Controller', 'Pawn', 'ObjectProperty', 8),
            ('pc_player', s.pc, 'PlayerController', 'Player', 'ObjectProperty', 8),
            ('pawn_pc', s.pawn, 'Pawn', 'Controller', 'ObjectProperty', 8),
            ('role', s.pawn, 'Actor', 'Role', 'ByteProperty', 1)):
        field = s.field(obj, cls, prop)
        if (field['type'], field['size']) != (kind, size) or not 0 < field['offset'] < 0x2000:
            raise RuntimeError('Unsupported native roll ownership layout')
        result[name] = field['offset']
    if result['pawn_pc'] != 0x2D8:
        raise RuntimeError('Native roll controller anchor changed')
    return result


class WindowsBackend:
    def __init__(self, game):
        self.game, self.kernel = game, game.kernel
        for name, args, result in (
                ('VirtualAllocEx', [W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, W.DWORD], C.c_void_p),
                ('VirtualFreeEx', [W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD], W.BOOL),
                ('VirtualProtectEx', [W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
                ('FlushInstructionCache', [W.HANDLE, C.c_void_p, C.c_size_t], W.BOOL),
                ('CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE),
                ('OpenThread', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
                ('SuspendThread', [W.HANDLE], W.DWORD), ('ResumeThread', [W.HANDLE], W.DWORD),
                ('GetThreadContext', [W.HANDLE, C.c_void_p], W.BOOL),
                ('GetThreadId', [W.HANDLE], W.DWORD),
                ('GetProcessIdOfThread', [W.HANDLE], W.DWORD),
                ('VirtualQueryEx', [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t], C.c_size_t)):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result

    def allocate_near(self):
        class Region(C.Structure):
            _fields_ = [('base', C.c_void_p), ('allocation', C.c_void_p),
                        ('allocation_protect', W.DWORD), ('partition', W.WORD),
                        ('size', C.c_size_t), ('state', W.DWORD),
                        ('protect', W.DWORD), ('kind', W.DWORD)]
        # Scan regions, rather than attempting thousands of occupied addresses.
        # Windows x64 allocation starts are aligned to 64 KiB.
        center = (self.game.base+0x5AAD000) & ~0xFFFF
        low, high = max(0x10000, center-0x60000000), min(0x7FFFFFFF0000, center+0x60000000)
        address, candidates = low, []
        for _ in range(20000):
            if address >= high:
                break
            region = Region()
            if self.kernel.VirtualQueryEx(self.game.handle, address, C.byref(region), C.sizeof(region)) != C.sizeof(region):
                raise C.WinError(C.get_last_error())
            end = region.base+region.size
            if end <= address:
                raise RuntimeError('Native roll allocation region did not advance')
            if region.state == 0x10000:  # MEM_FREE.
                start = (max(low, region.base)+0xFFFF) & ~0xFFFF
                last = (min(high, end)-PAGE*2) & ~0xFFFF
                if start <= last:
                    candidates.append(max(start, min(center, last)))
            address = end
        else:
            raise RuntimeError('Native roll allocation scan exceeded its bound')
        for address in sorted(candidates, key=lambda value: abs(value-center)):
            pointer = self.kernel.VirtualAllocEx(self.game.handle, address, PAGE*2, 0x3000, 4)
            if pointer:
                return pointer
        raise RuntimeError('No near executable allocation is available for native roll')

    def protect(self, address, size, protection):
        old = W.DWORD()
        if not self.kernel.VirtualProtectEx(self.game.handle, address, size, protection, C.byref(old)):
            raise C.WinError(C.get_last_error())
        return old.value

    def flush(self, address, size):
        if not self.kernel.FlushInstructionCache(self.game.handle, address, size):
            raise C.WinError(C.get_last_error())

    def free_unpublished(self, address):
        if not self.kernel.VirtualFreeEx(self.game.handle, address, 0, 0x8000):
            raise C.WinError(C.get_last_error())

    def thread_ids(self):
        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('tid', W.DWORD),
                        ('pid', W.DWORD), ('base_priority', W.LONG),
                        ('delta_priority', W.LONG), ('flags', W.DWORD)]
        for name in ('Thread32First', 'Thread32Next'):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = [W.HANDLE, C.POINTER(Entry)], W.BOOL
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            entry = Entry(); entry.size = C.sizeof(entry)
            result = []
            present = self.kernel.Thread32First(snapshot, C.byref(entry))
            if not present:
                raise C.WinError(C.get_last_error())
            while present:
                if entry.pid == self.game.pid:
                    result.append(entry.tid)
                present = self.kernel.Thread32Next(snapshot, C.byref(entry))
            if C.get_last_error() != 18:
                raise C.WinError(C.get_last_error())
            return set(result)
        finally:
            self.kernel.CloseHandle(snapshot)

    @contextlib.contextmanager
    def frozen(self):
        handles = {}
        try:
            for _ in range(4):
                ids = self.thread_ids()
                if not ids:
                    raise RuntimeError('Game has no live threads')
                new = ids-handles.keys()
                if not new:
                    break
                for tid in new:
                    handle = self.kernel.OpenThread(0x42 | 8, False, tid)
                    if not handle:
                        raise C.WinError(C.get_last_error())
                    suspended = False
                    try:
                        if (self.kernel.GetThreadId(handle) != tid or
                                self.kernel.GetProcessIdOfThread(handle) != self.game.pid):
                            raise RuntimeError('Native roll thread identity changed')
                        if self.kernel.SuspendThread(handle) == 0xFFFFFFFF:
                            raise C.WinError(C.get_last_error())
                        suspended = True
                        handles[tid] = handle
                    finally:
                        if not suspended:
                            self.kernel.CloseHandle(handle)
            else:
                raise RuntimeError('Game threads changed while preparing native roll patch')
            rips = []
            for handle in handles.values():
                # AMD64 CONTEXT_CONTROL: 16-byte aligned, flags +30, RIP +F8.
                storage = C.create_string_buffer(0x4D0+16)
                pointer = (C.addressof(storage)+15) & ~15
                C.c_uint32.from_address(pointer+0x30).value = 0x100001
                if not self.kernel.GetThreadContext(handle, pointer):
                    raise C.WinError(C.get_last_error())
                rips.append(C.c_uint64.from_address(pointer+0xF8).value)
            yield rips
        finally:
            failures = []
            for tid, handle in reversed(list(handles.items())):
                if self.kernel.ResumeThread(handle) == 0xFFFFFFFF:
                    failures.append(tid)
                self.kernel.CloseHandle(handle)
            if failures:
                raise RuntimeError('Could not resume native roll installation threads')


class NativeRollHook:
    patches = PATCHES
    guards = GUARDS
    def __init__(self, game, backend=None):
        self.game = game
        self.backend = backend if backend is not None else WindowsBackend(game)
        self.allocation, self.config = None, None
        self.installed, self.published = False, False
        self.desired, self.offsets = {}, None
        self.active = False
        self.restore_failures = set()
        # Retain only outcomes of our own attempted writes and the original
        # page protection. F5 may retry these after an incomplete rollback.
        self.pending_restores = {}

    def verify(self, allow_mixed=False):
        for rva, original in self.patches.items():
            current = self.game.read(self.game.base+rva, 5)
            accepted = {original, self.desired.get(rva)} if allow_mixed else {
                self.desired[rva] if self.installed else original}
            if allow_mixed and rva in self.pending_restores:
                accepted.update(self.pending_restores[rva]['bytes'])
            if current not in accepted:
                raise RuntimeError('Native roll instruction differs from the verified build')
        for rva, expected in self.guards.items():
            if self.game.read(self.game.base+rva, len(expected)) != expected:
                raise RuntimeError('Native roll call-site guard changed')

    def _safe_rips(self, rips):
        ranges = [(self.game.base+0x5AAD4B0, self.game.base+0x5AAD7F3),
                  (self.game.base+0x5A997D0, self.game.base+0x5A9996A)]
        if self.allocation:
            ranges.append((self.allocation, self.allocation+PAGE))
        return not any(start <= rip < end for rip in rips for start, end in ranges)

    def _replace(self, installing, session=None):
        deadline = time.monotonic()+1.
        previous_installed = self.installed
        recovery_protections = {rva & ~(PAGE-1): record['protection']
                                for rva, record in self.pending_restores.items()}
        while True:
            with self.backend.frozen() as rips:
                if self._safe_rips(rips):
                    if session is not None:
                        session.local_scope.validate()
                    self.verify(allow_mixed=not installing)
                    changed = []
                    try:
                        for rva, original in self.patches.items():
                            address = self.game.base+rva
                            before = self.game.read(address, 5)
                            desired = self.desired[rva] if installing else original
                            previous_protection = self.backend.protect(address, 5, 0x40)
                            protection = recovery_protections.get(rva & ~(PAGE-1), previous_protection)
                            # Record before attempting the write, including partial failures.
                            owned = write_prefixes(before, desired)
                            changed.append((rva, address, before, protection, owned))
                            self.game.write(address, desired)
                            self.backend.flush(address, 5)
                            if self.game.read(address, 5) != desired:
                                raise RuntimeError('Native roll instruction write did not persist')
                            self.backend.protect(address, 5, protection)
                        self.installed = installing
                        self.verify()
                        self.pending_restores.clear()
                        return
                    except BaseException:
                        rollback_failures = []
                        for rva, address, before, protection, owned in reversed(changed):
                            try:
                                current = self.game.read(address, 5)
                                if current not in owned:
                                    raise RuntimeError('Native roll rollback found foreign code')
                                self.backend.protect(address, 5, 0x40)
                                owned.update(write_prefixes(current, before))
                                self.game.write(address, before)
                                self.backend.flush(address, 5)
                                self.backend.protect(address, 5, protection)
                                if self.game.read(address, 5) != before:
                                    raise RuntimeError('Native roll rollback readback failed')
                            except (OSError, RuntimeError, ValueError):
                                rollback_failures.append(address)
                                prior = self.pending_restores.get(rva)
                                if prior is not None:
                                    owned.update(prior['bytes'])
                                self.pending_restores[rva] = dict(bytes=owned, protection=protection)
                        if rollback_failures:
                            self.installed = True  # A mixed patch remains recoverable.
                            self.restore_failures.update(rollback_failures)
                            raise RuntimeError('Native roll patch rollback was incomplete')
                        self.installed = previous_installed
                        raise
            if time.monotonic() >= deadline:
                raise RuntimeError('Native roll activation remained busy; patch not changed')
            time.sleep(.005)

    def prepare(self, session):
        if not NATIVE_ROLL_AVAILABLE:
            raise RuntimeError('Native roll experiment disabled after reversed co-op travel and a game exit')
        if self.pending_restores:
            raise RuntimeError('Native roll restoration is pending; use F5 before reactivation')
        offsets = layout_offsets(session)
        if self.offsets is not None and offsets != self.offsets:
            raise RuntimeError('Native roll ownership layout changed')
        self.verify()
        if self.installed:
            return
        self.offsets = offsets
        if self.allocation is not None:
            # A restored, published stub stays immutable and is reused for this
            # helper's later first-person sessions. Do not leak a new page/menu.
            self._replace(True, session)
            return
        self.allocation = self.backend.allocate_near()
        self.config = self.allocation+PAGE
        try:
            bool_address, enum_address = self.allocation, self.allocation+0x400
            first = direction_stub(bool_address, self.config, self.game.base+BOOL_GETTER, offsets)
            second = stick_stub(enum_address, self.config, offsets)
            if len(first) > 0x400 or len(second) > 0x400:
                raise RuntimeError('Native roll stub exceeds its reserved block')
            self.game.write(self.allocation, first)
            self.game.write(enum_address, second)
            self.game.write(self.config, bytes(CONFIG_SIZE))
            if self.game.read(self.allocation, len(first)) != first or self.game.read(enum_address, len(second)) != second:
                raise RuntimeError('Native roll stub readback failed')
            self.backend.protect(self.allocation, PAGE, 0x20)  # Code RX; config stays RW.
            self.backend.flush(self.allocation, PAGE)
            self.desired = {rva: relative_branch(0xE8, self.game.base+rva,
                            enum_address if rva == 0x5A997FB else bool_address) for rva in PATCHES}
            # Retain any block that might have been published, even on a failure.
            self.published = True
            self._replace(True, session)
            print('Native roll prototype installed with local identity/possession gates.', flush=True)
        except BaseException:
            if not self.published:
                self.backend.free_unpublished(self.allocation)
                self.allocation = self.config = None
            raise

    def activate(self, session):
        self.prepare(session)
        session.local_scope.validate()
        raw = scope_config(session)
        self.deactivate()
        self.game.write(self.config, raw[:64])
        self.game.write(self.config+80, raw[80:])
        session.local_scope.validate()
        self.game.write(self.config, struct.pack('<I', 1))
        if self.game.read(self.config, 4) != struct.pack('<I', 1):
            raise RuntimeError('Native roll local gate did not activate')
        self.active = True

    def deactivate(self):
        if self.config is not None:
            self.game.write(self.config, bytes(4))
            if self.game.read(self.config, 4) != bytes(4):
                raise RuntimeError('Native roll local gate did not deactivate')
        self.active = False

    def counters(self):
        if self.config is None:
            return (0, 0, 0)
        return struct.unpack('<III', self.game.read(self.config+64, 12))

    def restore(self):
        self.restore_failures.clear()
        try:
            self.deactivate()
            if self.installed:
                self._replace(False)
            self.verify()
        except (OSError, RuntimeError, ValueError) as error:
            self.restore_failures.add(str(error))
        # Never free a published stub: a native return may still refer to it.
        # Its inactive code/config is at most 8 KiB and retires with the process.
        print(f'Native roll instructions: {len(self.restore_failures)} restoration failures.', flush=True)


class NativeRollProfile:
    def __init__(self, session):
        self.session = session
        self.eligible, self.applied, self.restore_failures = True, False, set()
        manager = getattr(session.game, 'native_roll_hook', None)
        if manager is None:
            manager = session.game.native_roll_hook = NativeRollHook(session.game)
        self.manager = manager

    def set_input(self, eligible):
        self.eligible = bool(eligible)

    def update(self):
        self.session.local_scope.validate()
        if self.eligible and not self.applied:
            self.applied = True  # Restoration covers partial activation.
            self.manager.activate(self.session)
        elif not self.eligible and self.applied:
            self.restore()
            if self.restore_failures:
                raise RuntimeError('Native roll gate restoration failed')

    def restore(self):
        self.restore_failures.clear()
        try:
            self.manager.deactivate()
            self.applied = False
        except (OSError, RuntimeError, ValueError) as error:
            self.restore_failures.add(str(error))
