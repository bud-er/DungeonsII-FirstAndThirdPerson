"""Optional native walking-parameter override for the validated local player.

The pinned native writer derives MaxWalkSpeed from a per-component parameter
and GAS attributes. Change the parameter once and let that writer compute the
speed; never compete with its output. Falling uses a separate native value.
No velocity, root motion, game code, or GAS attributes are changed. Native
network acceptance of a local parameter change still requires gameplay testing.
"""
from native_layout import matches as native_matches, rva as native_rva

import math
import struct
import time
from collections import deque
from native_combat_activity import NativeCombatActivity, ActivityUnavailable


NATIVE_GUARDS = {
    0x63B1F30: bytes.fromhex('80b931020000030f85d3327bfdf30f1081a4120000c3'),
    0x3B6525A: bytes.fromhex('f30f1083780200004883c4205bc3'),
    0x3C64330: bytes.fromhex('80b939030000000f97c0c3'),
    # Writer takes the parameter block in rdx, with ASC at movement+0x12b0.
    0x63A707E: bytes.fromhex('488bea488bf9488bb1b0120000'),
    # MaxWalkSpeed = native attribute * parameter[0] * native multiplier.
    0x63A70F3: bytes.fromhex('f30f597d00f30f59fef30f11bf78020000f30f108f9c120000'),
    # BeginPlay copies default local parameters into the active parameter block.
    0x63A89E0: bytes.fromhex('0f10814c1200000f1181601200008b815c120000'),
    # Player virtual slot390 calls that writer with movement+0x1260.
    0x63BC399: bytes.fromhex('4883b9b012000000488bd90f842e020000488d91601200004889bc24c0000000e8a2acfeff'),
}
REFRESH_LIMIT = 4
REFRESH_WINDOW_SECONDS = .25
CONTENTION_REASON = 'game repeatedly resets walking parameter; sprint paused for this Helper session'
SUPPORTED_PARAMETER = struct.pack('<f', 1.)


class SprintProfile:
    def __init__(self, session, *, multiplier=1.2):
        if isinstance(multiplier, bool) or not math.isfinite(multiplier) or not 1.05 <= multiplier <= 1.35:
            raise ValueError('Sprint multiplier must be between 1.05 and 1.35')
        self.session, self.game = session, session.game
        self.multiplier = multiplier
        self.requested = self.eligible = self.interrupted = False
        self.saved = self.written = None
        self.restore_failures = set()
        self.reason = 'idle'
        self.native_refreshes = 0
        self.last_observed_speed = None
        self.last_observed_parameter = None
        self.last_stop_reason = None
        self.activations = 0
        self.refresh_times = deque(maxlen=REFRESH_LIMIT)
        self.paused_reason = getattr(self.game, '_sprint_contention_reason', None)
        for name in ('Character', 'CharacterMovementComponent'):
            session.ensure_layout(name)
        self.movement = session.pointer(session.pawn, 'Character', 'CharacterMovement')
        session.remember(self.movement, 'PlayerCharacterMovementComponent')
        self.identity = session.identities[self.movement]
        self.speed = self._field('MaxWalkSpeed', 'FloatProperty', 0x278, 4)
        self.mode = self._field('MovementMode', 'ByteProperty', 0x231, 1)
        # Unreflected layout is pinned by the native code guards below.
        self.parameter = self.movement+0x1260
        self.default_parameter = self.movement+0x124C
        for rva, expected in NATIVE_GUARDS.items():
            if not native_matches(self.game, rva, expected):
                raise RuntimeError('Sprint native movement layout check failed')
        vtable = self.game.u64(self.movement)
        if self.game.u64(vtable+157*8) != self.game.base+native_rva(self.game, 0x63B1F30):
            raise RuntimeError('Unsupported player movement-speed getter')
        if self.game.u64(vtable+390*8) != self.game.base+native_rva(self.game, 0x63BC390):
            raise RuntimeError('Unsupported player movement-parameter writer')
        self.activity = NativeCombatActivity(session)
        self.validate()

    def _field(self, name, kind, offset, size):
        prop = self.session.field(self.movement, 'CharacterMovementComponent', name)
        if (prop['type'], prop['offset'], prop['size'], prop['dimensions']) != (kind, offset, size, 1):
            raise RuntimeError(f'Unsupported sprint field: {name}')
        return prop['address']

    @property
    def applied(self):
        return self.written is not None

    def validate(self):
        s = self.session
        s.local_scope.validate()
        if s.identity(self.movement) != self.identity or \
                s.pointer(s.pawn, 'Character', 'CharacterMovement') != self.movement or \
                s.reflection.header(self.movement)['outer'] != s.pawn:
            raise RuntimeError('Sprint movement ownership changed')

    def set_input(self, requested, *, eligible=True):
        self.requested, self.eligible = bool(requested), bool(eligible)
        self.interrupted = False

    def _stop(self, *, interrupted=False, reason='idle'):
        if interrupted and self.requested:
            self.last_stop_reason = reason
            print(f'Sprint stopped: {reason}.', flush=True)
        self.restore()
        self.interrupted = interrupted
        self.reason = reason
        if self.restore_failures:
            raise RuntimeError('Sprint parameter restoration failed')

    def update(self):
        self.validate()
        if self.paused_reason:
            self._stop(interrupted=self.requested, reason=self.paused_reason)
            return
        if not self.requested or not self.eligible:
            self._stop()
            return
        try:
            attack, roll = self.activity.snapshot()
        except ActivityUnavailable:
            self._stop(interrupted=True, reason='native activity unavailable')
            return
        reason = ('native attack' if attack else 'native roll' if roll else
                  'not walking' if self.game.read(self.mode, 1) != b'\x01' else
                  'native movement lock' if self.game.read(self.session.pc+0x339, 1) != b'\0' else None)
        if reason:
            self._stop(interrupted=True, reason=reason)
            return
        current = self.game.read(self.parameter, 4)
        self.last_observed_parameter = struct.unpack('<f', current)[0]
        self.last_observed_speed = struct.unpack('<f', self.game.read(self.speed, 4))[0]
        if self.game.read(self.default_parameter, 4) != SUPPORTED_PARAMETER:
            self._stop(interrupted=True, reason='unsupported native walking default')
            return
        if not math.isfinite(self.last_observed_speed) or not 1 <= self.last_observed_speed <= 4050:
            self._stop(interrupted=True, reason='invalid walking speed')
            return
        if self.applied:
            if current == self.written:
                return
            if current != self.saved:
                # Native parameter changes win; do not adopt or compound them.
                self._stop(interrupted=True, reason='native walking parameter changed')
                return
            # A sporadic refresh may retry the same fixed parameter. Repeated
            # contention pauses the feature before another rewrite.
            self.native_refreshes += 1
            now = time.monotonic()
            if self.refresh_times and now < self.refresh_times[-1]:
                self.refresh_times.clear()
            while self.refresh_times and now-self.refresh_times[0] > REFRESH_WINDOW_SECONDS:
                self.refresh_times.popleft()
            self.refresh_times.append(now)
            if len(self.refresh_times) >= REFRESH_LIMIT:
                # Four exact-baseline reversions in 250 ms are parameter
                # contention, not proof of network correction. Stop rewriting
                # before this refresh; leave the native baseline in place.
                # Latch on the game connection so menu/camera recreation does
                # not silently start the same contention again.
                self.paused_reason = CONTENTION_REASON
                self.game._sprint_contention_reason = CONTENTION_REASON
                self._stop(interrupted=True, reason=CONTENTION_REASON)
                return
            desired = self.written
        else:
            if current != SUPPORTED_PARAMETER:
                self._stop(interrupted=True, reason='unsupported native walking parameter')
                return
            if not math.isfinite(self.last_observed_speed) or not 1 <= self.last_observed_speed <= 3000:
                self._stop(interrupted=True, reason='invalid walking speed')
                return
            desired = struct.pack('<f', self.last_observed_parameter*self.multiplier)
        self.validate()
        if self.game.read(self.parameter, 4) != current or \
                self.game.read(self.default_parameter, 4) != SUPPORTED_PARAMETER or \
                self.game.read(self.mode, 1) != b'\x01' or \
                self.game.read(self.session.pc+0x339, 1) != b'\0':
            self._stop(interrupted=True, reason='movement state changed')
            return
        # Record before the write so partial errors can still restore it.
        if not self.applied:
            self.activations += 1
        self.saved, self.written = current, desired
        self.reason = 'native walking parameter override requested'
        self.game.write(self.parameter, desired)
        observed = self.game.read(self.parameter, 4)
        self.last_observed_parameter = struct.unpack('<f', observed)[0]
        if observed not in (desired, self.saved):
            self._stop(interrupted=True, reason='native walking parameter changed')

    def diagnostics(self):
        value = self.last_observed_speed
        return dict(requested=self.requested, applied=self.applied, reason=self.reason,
                    native_refreshes=self.native_refreshes, activations=self.activations,
                    last_stop_reason=self.last_stop_reason,
                    paused_reason=self.paused_reason,
                    refreshes_in_window=len(self.refresh_times),
                    observed_speed=value if value is not None and math.isfinite(value) else None,
                    observed_parameter=self.last_observed_parameter if self.last_observed_parameter is not None
                        and math.isfinite(self.last_observed_parameter) else None,
                    target_parameter=struct.unpack('<f', self.written)[0] if self.written else None)

    def restore(self):
        self.requested = self.eligible = False
        self.reason = 'idle'
        self.restore_failures = set()
        if not self.applied:
            return
        try:
            retired = self.session.object_retired(self.movement, self.identity)
            if retired is None:
                raise RuntimeError('Sprint object lifetime could not be checked')
            if not retired:
                if self.session.identity(self.movement) != self.identity or \
                        self.session.reflection.header(self.movement)['outer'] != self.session.pawn:
                    raise RuntimeError('Sprint object changed before restoration')
                current = self.game.read(self.parameter, 4)
                self.last_observed_parameter = struct.unpack('<f', current)[0]
                if current == self.written:
                    self.game.write(self.parameter, self.saved)
                    if self.game.read(self.parameter, 4) != self.saved:
                        raise RuntimeError('Sprint parameter restore did not persist')
                    self.last_observed_parameter = struct.unpack('<f', self.saved)[0]
            self.saved = self.written = None
        except (OSError, RuntimeError, ValueError):
            self.restore_failures.add(self.parameter)
