"""Keep consumed native movement input supplied independently of scene scans.

Only the validated pawn's pending input vector is written. Physical input is
sampled afresh on every pulse; a short lease from the main loop, focus, native
move locks, possession and mapping ownership all gate the write. Stop joins
the worker before normal input/camera restoration or process-handle closure.
"""
import ctypes as C
from ctypes import wintypes as W
import math
import threading
import time
import multiprocessing as mp
import pickle
import struct


def deadzoned_axis(value):
    amount = min(abs(value) / 32767, 1.)
    return math.copysign(min(1., max(0., (amount-.18)/.82)), value)


class PhysicalMovement:
    """Read-only key/controller sampling; no hotkeys, cursor warps or injection."""
    def __init__(self, pid):
        self.pid = pid
        self.user = C.WinDLL('user32')
        self.user.GetForegroundWindow.restype = W.HWND
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.GetAsyncKeyState.argtypes = [C.c_int]
        self.user.GetAsyncKeyState.restype = C.c_short
        class Pad(C.Structure):
            _fields_ = [('buttons', W.WORD), ('lt', W.BYTE), ('rt', W.BYTE),
                        ('lx', C.c_short), ('ly', C.c_short), ('rx', C.c_short), ('ry', C.c_short)]
        class State(C.Structure):
            _fields_ = [('packet', W.DWORD), ('pad', Pad)]
        self.State = State
        self.xinput = C.WinDLL('xinput1_4')
        self.xinput.XInputGetState.argtypes = [W.DWORD, C.POINTER(State)]
        self.xinput.XInputGetState.restype = W.DWORD

    def sample(self):
        from keyboard_movement import keyboard_axes, controller_axes
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(self.user.GetForegroundWindow(), C.byref(pid))
        if pid.value != self.pid:
            return None
        state = self.State()
        for index in range(4):
            if self.xinput.XInputGetState(index, C.byref(state)) == 0:
                p = state.pad
                axes = tuple(deadzoned_axis(v) for v in (p.lx, p.ly, p.rx, p.ry))
                if p.buttons or p.lt > 30 or p.rt > 30 or any(axes):
                    return controller_axes(axes[:2])
                break  # Same first connected pad as the main camera input.
        return keyboard_axes(*(bool(self.user.GetAsyncKeyState(k) & 0x8000)
                               for k in (0x57, 0x41, 0x53, 0x44)))


class MovementPulse:
    INTERVAL = .002
    LEASE = .075

    def __init__(self, profile, sampler=None, *, clock=time.monotonic):
        self.profile = profile
        self.sampler = sampler if sampler is not None else PhysicalMovement(profile.game.pid)
        self.clock = clock
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.deadline = 0.
        self.yaw = 0.
        self.records = ()
        self.pending = None
        self.error = None
        self.writes = self.expirations = 0

    def renew(self, yaw, records):
        from keyboard_movement import movement_vector
        movement_vector(yaw, (0., 0.))
        with self.lock:
            if self.error is not None:
                raise RuntimeError(f'Movement pulse stopped: {self.error}') from self.error
            self.yaw, self.records = yaw, tuple(records)
            self.deadline = self.clock() + self.LEASE
            if self.thread is None:
                self.stopping.clear()
                self.thread = threading.Thread(target=self._run, name='native-movement-input', daemon=True)
                self.thread.start()

    def _clear(self):
        if self.pending is None:
            return
        p = self.profile
        p.validate()
        if p.game.read(p.address, 24) == self.pending:
            p.game.write(p.address, bytes(24))
        self.pending = None

    def pause(self):
        with self.lock:
            self.deadline = 0.
            self._clear()

    def step(self):
        from keyboard_movement import movement_vector
        with self.lock:
            if self.stopping.is_set() or self.error is not None:
                return
            now = self.clock()
            if not self.deadline or now >= self.deadline or now < self.deadline-self.LEASE:
                if self.deadline:
                    self.expirations += 1
                self.deadline = 0.
                self._clear()
                return
            p, s = self.profile, self.profile.session
            axes = self.sampler.sample()
            if axes is None or axes == (0., 0.):
                self._clear()
                return
            # These checks stay fresh on every pulse, independently of slower
            # discovery/binding scans. Never reuse a per-frame identity cache.
            p.validate()
            s.local_scope.validate()
            if (s.input_paused or s.pointer(s.pawn, 'PlayerCharacter', 'CurrentCamera') != s.camera or
                    s.pointer(s.camera, 'SceneComponent', 'AttachParent') != s.root or
                    p.game.read(s.pc+0x339, 1) != b'\0'):
                self._clear()
                return
            for record in self.records:
                if not record.valid() or p.game.read(record.row+0x28, 8) != bytes(8):
                    self.deadline = 0.
                    self._clear()
                    return
            value = movement_vector(self.yaw, axes)
            # Publish first so a partial/failed write is still covered by restore.
            self.pending = value
            p.game.write(p.address, value)
            self.writes += 1

    def _run(self):
        while not self.stopping.is_set():
            started = self.clock()
            try:
                self.step()
            except Exception as error:
                with self.lock:
                    self.error, self.deadline = error, 0.
                self.stopping.set()
                return
            # Event.wait uses the coarse Windows wait timeout (~15 ms on this
            # machine). Python's sleep uses a high-resolution waitable timer.
            # Stop still joins after at most this 2 ms sleep plus one pulse.
            time.sleep(max(0., self.INTERVAL-(self.clock()-started)))

    def stop(self):
        self.stopping.set()
        thread = self.thread
        if thread is not None:
            thread.join()  # Only bounded Windows reads/writes; never wait on the main loop.
            self.thread = None
        self.pause()

    def diagnostics(self):
        with self.lock:
            return dict(writes=self.writes, lease_expirations=self.expirations,
                        running=self.thread is not None and self.thread.is_alive(),
                        error=str(self.error) if self.error else None)


def _record_payload(record):
    return {name: getattr(record, name) for name in
            ('owner', 'header', 'identity', 'descriptor', 'row', 'original', 'tag', 'tail')}


def movement_process(expected, mailbox, stopping, stats, errors, reader_factory=None, sampler_factory=None):
    """Separate Python interpreter: memory reads cannot contend for camera GIL."""
    from external_camera import CameraMemory, CameraSession
    from game_memory import _process_identity
    from keyboard_movement import KeyboardMovement, DisabledKey
    pulse = game = None
    try:
        game = (reader_factory or CameraMemory)(pid=expected['pid'])
        if _process_identity(game.kernel, game.handle) != expected['process_identity']:
            raise RuntimeError('Movement process identity changed')
        if stopping.is_set(): return
        # Rebuild only the local scope from freshly validated reflected classes.
        # A second global object scan would stall startup for several seconds.
        from inspect_camera import Reflection
        from local_player_scope import LocalPlayerScope
        session = CameraSession.__new__(CameraSession)
        session.game, session.reflection = game, Reflection(game)
        session.native = expected['native']
        session.layouts, session.ancestors = {}, {}
        session.identities, session.identity_kinds = {}, {}
        session.input_paused = False
        for cls in session.native:
            session.ensure_layout(cls)
        kinds = dict(pc='GameplayPlayerController',pawn='PlayerCharacter',root='SceneComponent',
                     camera='CameraComponent',movement='CharacterMovementComponent',player='LocalPlayer')
        for name, (address, identity) in expected['objects'].items():
            setattr(session,name,address)
            session.remember(address,kinds[name])
            if session.identities[address] != identity:
                raise RuntimeError('Movement player/component changed during worker setup')
        session.local_scope = LocalPlayerScope(session)
        if session.local_scope.player != expected['objects']['player'][0]:
            raise RuntimeError('Movement local player changed')
        profile = KeyboardMovement.__new__(KeyboardMovement)
        profile.session, profile.game = session, game
        profile.movement = expected['objects']['movement'][0]
        session.identities[profile.movement] = expected['objects']['movement'][1]
        profile.address = session.field(session.pawn, 'Pawn', 'ControlInputVector')['address']
        if profile.address != expected['address']:
            raise RuntimeError('Movement input address changed')
        pulse = MovementPulse(profile, sampler=sampler_factory(game.pid) if sampler_factory else None)
        parent, seen = mp.parent_process(), -1
        stats[0] = 1.  # Ready. No pending input has been written yet.
        while not stopping.is_set() and (parent is None or parent.is_alive()):
            started = time.monotonic()
            # This lock is held only while copying a bounded message, never
            # during memory access, discovery or native calls.
            with mailbox.get_lock():
                raw = mailbox.get_obj()
                revision, length = struct.unpack('<QI', bytes(raw[:12]))
                data = bytes(raw[12:12+length]) if revision != seen else None
            if data is not None:
                message = pickle.loads(data)  # Private anonymous parent/child mailbox.
                pulse.deadline, pulse.yaw = message['deadline'], message['yaw']
                records = []
                for item in message['records']:
                    record = DisabledKey.__new__(DisabledKey)
                    record.__dict__.update(item)
                    record.profile = profile
                    records.append(record)
                pulse.records, seen = tuple(records), revision
            pulse.step()
            # Publish an acknowledgement after the pulse/clear has completed.
            stats[1], stats[2], stats[3] = pulse.writes, pulse.expirations, seen
            stats[4] = float(pulse.pending is not None)
            time.sleep(max(0., pulse.INTERVAL-(time.monotonic()-started)))
    except Exception as error:
        try: errors.send(f'{type(error).__name__}: {error}'[:1000])
        except (BrokenPipeError, OSError): pass
    finally:
        try:
            if pulse is not None: pulse.pause()
            stats[4] = 0.
        except Exception as error:
            try: errors.send(f'Movement restore: {type(error).__name__}: {error}'[:1000])
            except (BrokenPipeError, OSError): pass
        finally:
            if game is not None: game.close()
            stats[0] = 0.
            errors.close()


class MovementProcess:
    """One latest-value mailbox, with acknowledged pause and cooperative stop."""
    def __init__(self, profile):
        self.profile = profile
        self.process = None
        self.context = mp.get_context('spawn')
        self.mailbox = self.context.Array('B', 32768)
        self.stopping = self.context.Event()
        self.stats = self.context.RawArray('d', 5)
        self.error = None
        self.revision = 0
        self.yaw, self.records = 0., ()
        self.errors = None

    def _check(self):
        if self.errors is not None:
            while True:
                try:
                    if not self.errors.poll(): break
                    self.error = self.errors.recv()
                except EOFError:
                    break
                except OSError as error:
                    if getattr(error, 'winerror', None) == 109: break
                    raise
        if self.error:
            raise RuntimeError('Movement worker: '+self.error)
        if self.process is not None and not self.process.is_alive():
            raise RuntimeError('Movement worker exited unexpectedly')

    def _publish(self, deadline):
        self.revision += 1
        data = pickle.dumps(dict(deadline=deadline, yaw=self.yaw, records=self.records), protocol=4)
        if len(data) > len(self.mailbox)-12:
            raise RuntimeError('Too many movement binding records')
        with self.mailbox.get_lock():
            raw = self.mailbox.get_obj()
            raw[:12] = struct.pack('<QI', self.revision, len(data))
            raw[12:12+len(data)] = data

    def describe_scope(self):
        from game_memory import _process_identity
        p, s = self.profile, self.profile.session
        return dict(pid=p.game.pid, process_identity=_process_identity(p.game.kernel, p.game.handle),
            address=p.address, objects={name:(address,s.identity(address)) for name,address in
                (('pc',s.pc),('pawn',s.pawn),('root',s.root),('camera',s.camera),('movement',p.movement),
                 ('player',s.local_scope.player))}, native={name:s.native[name] for name in
                    ('Actor','Pawn','Character','Controller','PlayerController','PlayerCharacter','SceneComponent')})

    def _start(self):
        expected = self.describe_scope()
        self.stopping.clear()
        self.stats[:] = [0.]*5
        self.errors, sender = self.context.Pipe(duplex=False)
        self.process = self.context.Process(target=movement_process,
            args=(expected,self.mailbox,self.stopping,self.stats,sender),
            name='Dungeons movement input', daemon=True)
        self.process.start()
        sender.close()

    def renew(self, yaw, records):
        from keyboard_movement import movement_vector
        movement_vector(yaw, (0.,0.))
        self._check()
        self.yaw, self.records = yaw, tuple(_record_payload(r) for r in records)
        self._publish(time.monotonic()+MovementPulse.LEASE)
        if self.process is None:
            self._start()

    def pause(self):
        if self.process is None: return
        self._publish(0.)
        # If setup has not reached readiness, it cannot have emitted input.
        # Otherwise wait until the worker has cleared this exact revision.
        if self.stats[0]:
            deadline = time.monotonic()+2.
            while self.stats[3] < self.revision or self.stats[4]:
                self._check()
                if time.monotonic() >= deadline:
                    self.stop()
                    raise RuntimeError('Movement pause acknowledgement timed out')
                time.sleep(.001)

    def stop(self):
        if self.process is None: return
        self._publish(0.)
        self.stopping.set()
        self.process.join(10.)
        if self.process.is_alive():
            raise RuntimeError('Movement worker has not finished restoration')
        self.process.close()
        self.process = None
        try:
            self._check()
        finally:
            self.errors.close()
            self.errors = None

    def diagnostics(self):
        return dict(running=self.process is not None and self.process.is_alive(),
                    ready=bool(self.stats[0]), writes=int(self.stats[1]),
                    lease_expirations=int(self.stats[2]), error=self.error)
