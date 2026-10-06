"""Camera-facing native combat for the fingerprinted local controller.

The engine turns the pawn from controller rotation. Movement vectors, physics
transforms and other players are never written. Melee target candidates are
suppressed only on this controller while first person is active. Ranged targets,
selected-target caches and bow timing stay native by default.
"""
import math
import struct
from locomotion_facing import control_yaw


# Native code checks anchor the unreflected melee weak cache.
NATIVE_GUARDS = {
    0x637D3CA: bytes.fromhex('488b89e00100004881c11c010000'),
}
TARGET_WEAK_OFFSETS = (0x11C,)


def aim_rotation(yaw, pitch):
    if not all(math.isfinite(v) for v in (yaw, pitch)) or abs(pitch) > 80:
        raise RuntimeError('Invalid camera aim')
    return struct.pack('<ddd', pitch, (yaw+180)%360-180, 0.)


class CombatProfile:
    def __init__(self, session, *, steer_roll=True, filter_ranged=False):
        self.session, self.game = session, session.game
        self.records, self.restore_failures = {}, set()
        self.applied = False
        for name in ('Pawn', 'Character', 'CharacterMovementComponent', 'AutoTargeter', 'ActorComponent'):
            session.ensure_layout(name)
        self.movement = session.pointer(session.pawn, 'Character', 'CharacterMovement')
        self.targeter = session.pointer(session.pc, 'GameplayPlayerController', 'AutoTargeter')
        session.remember(self.movement, 'CharacterMovementComponent')
        session.remember(self.targeter, 'AutoTargeter')
        if session.layouts['AutoTargeter']['size'] != 0x340:
            raise RuntimeError('Unsupported automatic targeting layout')
        if session.reflection.header(self.targeter)['outer'] != session.pc:
            raise RuntimeError('Targeter does not belong to the local controller')
        for rva, expected in NATIVE_GUARDS.items():
            if self.game.read(self.game.base+rva, len(expected)) != expected:
                raise RuntimeError('Combat direction native layout check failed')
        for obj, cls, name, enabled in (
                (session.pawn, 'Pawn', 'bUseControllerRotationYaw', True),
                (self.movement, 'CharacterMovementComponent', 'bUseControllerDesiredRotation', False),
                (self.movement, 'CharacterMovementComponent', 'bOrientRotationToMovement', False)):
            prop = session.field(obj, cls, name)
            bits = prop['bool']
            if prop['size'] != 1 or bits['fieldSize'] != 1 or bits['byteOffset'] != 0:
                raise RuntimeError('Unexpected combat orientation flag')
            mask = bits['byteMask']
            self.record(obj, prop['address'], 1, mask, mask if enabled else 0)
        self.control = session.field(session.pc, 'Controller', 'ControlRotation')
        if (self.control['type'], self.control['size'], self.control.get('target')) != ('StructProperty', 24, 'Rotator'):
            raise RuntimeError('Unsupported controller aim rotation')
        self.saved_control = self.game.read(self.control['address'], 24)
        if not all(math.isfinite(v) for v in struct.unpack('<ddd', self.saved_control)):
            raise RuntimeError('Invalid native control rotation')
        from native_ranged_facing import NativeRangedFacing
        self.ranged_facing = NativeRangedFacing(session)
        self.ranged_handoff_active = False
        self.movement_facing_enabled = bool(steer_roll)
        self.axes, self.input_eligible, self.attack_held = (0., 0.), False, False
        self.facing_mode = 'camera'
        from native_combat_activity import NativeCombatActivity
        self.combat_activity = NativeCombatActivity(session) if steer_roll else None
        # Native forward dodge must commit the intended bearing itself. Editing
        # its force afterwards disagrees with the co-op server's target data.
        # Retain the field for diagnostics/older integrations, never construct
        # or update the legacy force writer in the supported control path.
        self.roll_direction = None
        from forward_targeting import ForwardTargeting
        self.forward_targeting = ForwardTargeting(session, self.targeter) if filter_ranged else None
        self.validate()

    def record(self, obj, address, size, mask, desired):
        if address not in self.records:
            self.records[address] = [obj, self.game.read(address, size), mask, desired]
        else:
            row = self.records[address]
            row[2] |= mask
            row[3] |= desired

    def validate(self):
        s = self.session
        for obj in (s.pc, s.pawn, s.root, self.movement, self.targeter):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Combat player/component changed')
        if s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn or \
                s.pointer(s.pawn, 'Character', 'CharacterMovement') != self.movement or \
                s.pointer(s.pc, 'GameplayPlayerController', 'AutoTargeter') != self.targeter:
            raise RuntimeError('Combat ownership changed')

    def set_input(self, buttons, left_stick, eligible=True, now=None, *, attack_held=False):
        # Validate before storing; these are the same axes used for movement.
        control_yaw(self.session.yaw,left_stick,eligible=False,movement_locked=False,
                    attack_active=False,roll_active=False)
        self.axes, self.input_eligible = left_stick, bool(eligible)
        self.attack_held = bool(attack_held)

    def update(self):
        from native_ranged_facing import RangedStateRebuilt
        from native_combat_activity import ActivityUnavailable
        self.validate()
        rotation = aim_rotation(self.session.yaw,self.session.pitch)
        # Both native input trees wait for the pawn to face the ranged target
        # before starting a shot. Do not overwrite that task's controller turn.
        # The first-person camera keeps its independent absolute look rotation.
        try:
            ranged_handoff = self.ranged_facing.waiting()
        except RangedStateRebuilt:
            # Input-method/tree handoffs can cross this external snapshot.
            # Defer combat writes for the frame while the camera keeps running.
            self.ranged_handoff_active = True
            self.facing_mode = 'input state rebuilding'
            return False
        if self.forward_targeting is not None:
            self.forward_targeting.validate()
        desired_yaw = self.session.yaw
        self.facing_mode = 'camera'
        if self.movement_facing_enabled:
            try:
                attack_active, roll_active = self.combat_activity.snapshot()
            except ActivityUnavailable:
                desired_yaw = None
                self.facing_mode = 'activity unavailable'
            else:
                movement_locked = self.game.read(self.session.pc+0x339,1) != b'\0'
                desired_yaw = control_yaw(self.session.yaw,self.axes,
                    eligible=self.input_eligible,movement_locked=movement_locked,
                    attack_active=attack_active or self.attack_held,roll_active=roll_active)
                self.facing_mode = ('native roll' if roll_active else
                    'native control' if desired_yaw is None else
                    'attack' if attack_active or self.attack_held else
                    'movement' if math.hypot(*self.axes)>.1 else 'camera')
        if ranged_handoff:
            desired_yaw = None
            self.facing_mode = 'native ranged turn'
        if desired_yaw is not None:
            rotation = aim_rotation(desired_yaw,self.session.pitch)
        self.validate()  # Recheck possession/targets after the read-only activity scan.
        self.applied = True  # Restoration also covers partial writes.
        if self.forward_targeting is not None:
            self.forward_targeting.update()
        for address, (_, _, mask, desired) in self.records.items():
            value = bytes([(self.game.read(address, 1)[0] & ~mask) | desired]) if mask else desired
            self.game.write(address, value)
        # These are weak references, never owning pointers or array storage.
        # Native candidates must not keep steering abilities toward a rear mob.
        for offset in TARGET_WEAK_OFFSETS:
            self.game.write(self.targeter+offset, bytes(8))
        self.ranged_handoff_active = ranged_handoff
        if desired_yaw is not None:
            self.game.write(self.control['address'], rotation)

    def restore(self):
        failures = set()
        self.ranged_handoff_active = False
        self.axes, self.input_eligible, self.attack_held = (0.,0.),False,False
        self.facing_mode = 'restored'
        if self.roll_direction is not None:
            self.roll_direction.clear()
        if not self.applied:
            self.restore_failures = failures
            return
        if self.forward_targeting is not None:
            self.forward_targeting.restore()
            failures.update(self.forward_targeting.restore_failures)
        for address, (obj, saved, mask, _) in reversed(list(self.records.items())):
            try:
                if self.session.identity(obj) != self.session.identities[obj]:
                    raise RuntimeError('Combat object changed')
                value = bytes([(self.game.read(address, 1)[0] & ~mask) | (saved[0] & mask)]) if mask else saved
                self.game.write(address, value)
                if self.game.read(address, len(value)) != value:
                    raise RuntimeError('Combat preference restore did not persist')
            except (OSError, RuntimeError, ValueError):
                failures.add(address)
        try:
            self.validate()
            # Hand control back at the pawn's current facing. Old target/rotation
            # snapshots would point at stale locations after moving in first person.
            native = self.session.value(self.session.root, 'SceneComponent', 'RelativeRotation', '<ddd')
            pitch, _, roll = struct.unpack('<ddd', self.saved_control)
            if not all(math.isfinite(v) for v in native):
                raise RuntimeError('Invalid pawn facing on combat handoff')
            value = struct.pack('<ddd', pitch, native[1], roll)
            self.game.write(self.control['address'], value)
            if self.game.read(self.control['address'], 24) != value:
                raise RuntimeError('Combat rotation handoff did not persist')
        except (OSError, RuntimeError, ValueError):
            failures.add('controller aim')
        self.restore_failures = failures
        if not failures:
            self.applied = False
        print(f'Combat aim: {len(failures)} restoration failures.', flush=True)
