"""Supply the local roll bearing before native target data/prediction commits.

Only two calls to the pawn-forward getter in GA_Roll are redirected. The
original ability, networking, cooldown, force creation and collision remain
native. No input-action remapping, root transform or force-source edit occurs.
"""
import math
import struct

from native_roll_hook import (NativeRollHook, Assembler, emit_gate, layout_offsets,
                              scope_config, serial_address, relative_branch, PAGE)
from roll_direction import stick_yaw
from build_info import ROLL_TARGET_DATA_AVAILABLE

FORWARD_GETTER = 0x3BB0130
PATCHES = {
    0x5AAD5C3: bytes.fromhex('e8682b10fe'),
    0x5AAD77C: bytes.fromhex('e8af2910fe'),
}
GUARDS = {
    0x5AAD545: bytes.fromhex('4c8bb7d8020000'),
    0x5AAD5B3: bytes.fromhex('488b8fb80100004885c9740b488d55e7'),
    0x5AAD76C: bytes.fromhex('488b8fb80100004885c9740b488d55c7'),
    0x5AAD5C8: bytes.fromhex('eb1c'),
    0x5AAD781: bytes.fromhex('eb1c'),
    # Also refuse the quarantined direction/stick experiment or foreign hooks.
    0x5AAD599: bytes.fromhex('e8d239d2ff'),
    0x5AAD752: bytes.fromhex('e81938d2ff'),
    0x5A997FB: bytes.fromhex('ba01000000'),
}
CONFIG_SIZE = 176
SEQUENCE = 136
VECTOR = 144


def roll_vector(yaw, axes):
    angle = math.radians(stick_yaw(yaw, *axes))
    return struct.pack('<ddd', math.cos(angle), math.sin(angle), 0.)


def vector_stub(address, config, original, offsets):
    a = Assembler(address)
    # Win64 volatile registers only, unchanged RSP, no calls/stack allocation.
    # RCX=root, RDX=output; activation retains RDI=pawn and R14=controller.
    emit_gate(a, config, 14, 7, offsets)
    a.emit('483b4860')  # Incoming root must match the saved local root.
    a.conditional(0x85, 'foreign')
    a.emit(bytes.fromhex('4c8b97')+struct.pack('<i', offsets['pawn_root']))
    a.emit('4c3bd1')  # Pawn still owns this root.
    a.conditional(0x85, 'foreign')
    a.emit('448b510c443b5068')  # Root UObject index.
    a.conditional(0x85, 'foreign')
    a.emit('4c8b5070458b12443b5078')  # Root FUObjectArray serial.
    a.conditional(0x85, 'foreign')
    a.emit('448b988800000041f6c301')  # Reject a writer's odd sequence.
    a.conditional(0x85, 'foreign')
    a.emit('0f108090000000f20f1088a0000000')  # Snapshot all three doubles.
    a.emit('443b9888000000')  # No mixed vector if an update overlapped.
    a.conditional(0x85, 'foreign')
    a.emit('833801')  # Disabling always hands back to the original getter.
    a.conditional(0x85, 'foreign')
    a.emit('f0ff80800000000f1102f20f114a10488bc2c3')
    a.label('foreign')
    a.emit('f0ff8084000000')
    a.branch(0xE9, original)
    return a.finish()


class RollTargetDataHook(NativeRollHook):
    patches, guards = PATCHES, GUARDS

    def prepare(self, session):
        if not ROLL_TARGET_DATA_AVAILABLE:
            raise RuntimeError('Roll target-data experiment disabled after a game exit in the live trial')
        if self.pending_restores:
            raise RuntimeError('Roll target-data restoration is pending; use F5')
        offsets = layout_offsets(session)
        root = session.field(session.pawn, 'Actor', 'RootComponent')
        if (root['type'], root['size'], root['offset']) != ('ObjectProperty', 8, 0x1B8):
            raise RuntimeError('Native roll root ownership layout changed')
        offsets['pawn_root'] = root['offset']
        if self.offsets is not None and offsets != self.offsets:
            raise RuntimeError('Roll target-data ownership layout changed')
        session.local_scope.validate()
        self.verify()
        if self.installed:
            return
        self.offsets = offsets
        if self.allocation is not None:
            self._replace(True, session)
            return
        self.allocation = self.backend.allocate_near()
        self.config = self.allocation+PAGE
        try:
            code = vector_stub(self.allocation, self.config,
                               self.game.base+FORWARD_GETTER, offsets)
            if len(code) > PAGE:
                raise RuntimeError('Roll target-data stub exceeds its reserved page')
            self.game.write(self.allocation, code)
            self.game.write(self.config, bytes(CONFIG_SIZE))
            if self.game.read(self.allocation, len(code)) != code:
                raise RuntimeError('Roll target-data stub readback failed')
            self.backend.protect(self.allocation, PAGE, 0x20)
            self.backend.flush(self.allocation, PAGE)
            self.desired = {rva: relative_branch(0xE8, self.game.base+rva,
                                              self.allocation) for rva in self.patches}
            self.published = True
            self._replace(True, session)
            print('Local roll target-data direction installed; late force editing disabled.', flush=True)
        except BaseException:
            if not self.published:
                self.backend.free_unpublished(self.allocation)
                self.allocation = self.config = None
            raise

    def activate(self, session, vector):
        self.prepare(session)
        self.deactivate()
        raw = scope_config(session)
        root = serial_address(session, session.root)
        self.game.write(self.config, raw[:64])
        self.game.write(self.config+80, raw[80:])
        self.game.write(self.config+96, struct.pack('<QIIQI', session.root,
                                                  root[0], 0, root[1], root[2]))
        self.publish_vector(vector)
        session.local_scope.validate()
        if session.pointer(session.pawn, 'Actor', 'RootComponent') != session.root:
            raise RuntimeError('Roll root changed during activation')
        self.game.write(self.config, struct.pack('<I', 1))
        if self.game.read(self.config, 4) != struct.pack('<I', 1):
            raise RuntimeError('Roll target-data gate did not activate')
        self.active = True

    def publish_vector(self, vector):
        values = struct.unpack('<ddd', vector)
        if not all(math.isfinite(v) for v in values) or abs(sum(v*v for v in values)-1.) > 1e-6:
            raise RuntimeError('Invalid roll target-data direction')
        sequence = self.game.u32(self.config+SEQUENCE)
        # Failed/partial publication stays odd; the leaf stub safely falls back.
        odd = (sequence+1) | 1
        self.game.write(self.config+SEQUENCE, struct.pack('<I', odd & 0xFFFFFFFF))
        self.game.write(self.config+VECTOR, vector)
        self.game.write(self.config+SEQUENCE, struct.pack('<I', (odd+1) & 0xFFFFFFFF))

    def counters(self):
        return struct.unpack('<II', self.game.read(self.config+128, 8)) if self.config else (0, 0)


class RollTargetDataProfile:
    def __init__(self, session):
        self.session = session
        self.axes, self.eligible = (0., 0.), False
        self.applied, self.restore_failures = False, set()
        manager = vars(session.game).get('roll_target_data_hook')
        if manager is None:
            manager = session.game.roll_target_data_hook = RollTargetDataHook(session.game)
        self.manager = manager

    def set_input(self, axes, eligible):
        roll_vector(self.session.yaw, axes)
        self.axes, self.eligible = axes, bool(eligible)

    def update(self):
        s = self.session
        s.local_scope.validate()
        if not self.eligible:
            self.restore()
            if self.restore_failures:
                raise RuntimeError('Roll target-data gate restoration failed')
            return
        vector = roll_vector(s.yaw, self.axes)
        if not self.applied:
            self.applied = True
            self.manager.activate(s, vector)
        else:
            self.manager.publish_vector(vector)

    def restore(self):
        self.restore_failures.clear()
        try:
            self.manager.deactivate()
            self.applied = False
        except (OSError, RuntimeError, ValueError) as error:
            self.restore_failures.add(str(error))
