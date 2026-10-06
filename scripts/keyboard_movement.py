"""Camera-relative WASD/left stick through the native pending input vector.

No transforms, velocities, physics or owning pointers are changed. The engine
consumes this vector normally. Keyboard binding conflicts are disabled by
changing only the FName of their FKey; its shared ownership/cache stays intact.
Native stick movement is replaced so the bow aim task cannot steer backward.
"""
import math
import struct
import time
from input_mapping_snapshot import mapping_snapshot, checked_row, MappingRebuilt


NATIVE_GUARDS = {
    0x40E37B5: bytes.fromhex('0f1089f0020000f20f108100030000'),
    0x3C64330: bytes.fromhex('80b939030000000f97c0c3'),
}
ZERO = bytes(24)


def keyboard_axes(w, a, s, d):
    x, y = float(bool(d))-float(bool(a)), float(bool(w))-float(bool(s))
    length = max(1., math.hypot(x, y))
    return x/length, y/length


def controller_axes(axes):
    x, y = axes
    if not all(math.isfinite(v) for v in (x,y)) or max(abs(x),abs(y)) > 1:
        raise RuntimeError('Invalid controller movement input')
    length = max(1., math.hypot(x,y))
    return x/length, y/length  # Preserve analog strength; normalize full diagonals.


def movement_vector(yaw, axes):
    x, y = axes
    if not all(math.isfinite(v) for v in (yaw, x, y)) or math.hypot(x, y) > 1.000001:
        raise RuntimeError('Invalid keyboard movement input')
    angle = math.radians(yaw)
    return struct.pack('<ddd', math.cos(angle)*y-math.sin(angle)*x,
                       math.sin(angle)*y+math.cos(angle)*x, 0.)


class DisabledKey:
    def __init__(self, profile, owner, header, descriptor, row, original, expected=None):
        self.profile, self.owner, self.header = profile, owner, header
        self.identity = profile.session.identity(owner)
        self.descriptor, self.row, self.original = descriptor, row, original
        raw = (profile.game.read(row, 0x50) if expected is None else
               checked_row(profile.game, header, descriptor, row, expected))
        # Ignore native trigger bookkeeping at +0x40. Keep action, arrays,
        # cached key ownership and the remaining row identity guarded.
        self.tag, self.tail = raw[:0x28], raw[0x30:0x40]+raw[0x41:]
        # A native context rebuild can copy the disabled name between our last
        # update and restoration. That copy already needs handoff restoration.
        self.applied = raw[0x28:0x30] == bytes(8) and original != bytes(8)

    def valid(self):
        p, g = self.profile, self.profile.game
        try:
            raw = g.read(self.row, 0x50)
            return (p.session.identity(self.owner) == self.identity and
                    g.read(self.header, 16) == self.descriptor and
                    raw[:0x28] == self.tag and raw[0x30:0x40]+raw[0x41:] == self.tail and
                    raw[0x28:0x30] in (self.original, bytes(8)))
        except (OSError, RuntimeError, ValueError):
            return False

    def disable(self):
        if not self.valid():
            return False
        self.applied = True
        if self.profile.game.read(self.row+0x28, 8) != bytes(8):
            self.profile.game.write(self.row+0x28, bytes(8))
        return True

    def restore(self):
        if not self.applied or not self.valid():
            return True  # Retired/rebuilt allocations must never be written.
        g = self.profile.game
        g.write(self.row+0x28, self.original)
        if g.read(self.row+0x28, 8) != self.original:
            return False
        self.applied = False
        return True


class KeyboardMovement:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.axes, self.eligible, self.pending = (0., 0.), False, None
        self.applied, self.restore_failures = False, set()
        self.bindings, self.source_keys, self.next_collect = {}, {}, 0.
        for name in ('Pawn', 'Character', 'CharacterMovementComponent', 'InputMappingContext',
                     'EnhancedPlayerInput', 'EnhancedActionKeyMapping', 'Key'):
            session.ensure_layout(name)
        field = session.field(session.pawn, 'Pawn', 'ControlInputVector')
        if (field['offset'], field['type'], field['size'], field.get('target')) != (0x2F0, 'StructProperty', 24, 'Vector'):
            raise RuntimeError('Unsupported native movement input layout')
        self.address = field['address']
        if session.layouts['EnhancedActionKeyMapping']['size'] != 0x50:
            raise RuntimeError('Unsupported keyboard mapping layout')
        key = next(p for p in session.layouts['EnhancedActionKeyMapping']['properties'] if p['name']=='Key')
        if (key['offset'], key['size']) != (0x28, 24):
            raise RuntimeError('Unsupported keyboard key layout')
        key_name = next(p for p in session.layouts['Key']['properties'] if p['name']=='KeyName')
        if session.layouts['Key']['size'] != 24 or \
                (key_name['type'], key_name['offset'], key_name['size']) != ('NameProperty', 0, 8):
            raise RuntimeError('Unsupported keyboard key-name layout')
        for rva, expected in NATIVE_GUARDS.items():
            if self.game.read(self.game.base+rva, len(expected)) != expected:
                raise RuntimeError('Native keyboard movement check failed')
        self.movement = session.pointer(session.pawn, 'Character', 'CharacterMovement')
        session.remember(self.movement, 'CharacterMovementComponent')
        self.sources = [o['address'] for o in session.objects if o['name'] in ('IMC_DefaultKBM','IMC_DefaultGamepad','IMC_SwitchGamepad')
                        and not o['flags']&0x30 and session.is_a(o['class'], 'InputMappingContext')]
        if sum(o['name']=='IMC_DefaultKBM' and o['address'] in self.sources for o in session.objects) != 1:
            raise RuntimeError('Expected the supported native keyboard context')
        self.collect()

    def validate(self):
        s = self.session
        for obj in (s.pc, s.pawn, self.movement):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Keyboard movement player/component changed')
        if s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn or \
                s.pointer(s.pawn, 'Pawn', 'Controller') != s.pc or \
                s.pointer(s.pawn, 'Character', 'CharacterMovement') != self.movement:
            raise RuntimeError('Keyboard movement ownership changed')

    def capture(self, owner, cls, name, source=False):
        s, g = self.session, self.game
        header = s.field(owner, cls, name)['address']
        descriptor, data = mapping_snapshot(g, header)
        pointer, count, capacity = struct.unpack('<Qii', descriptor)
        for i in range(count):
            row = pointer+i*0x50
            raw = data[i*0x50:(i+1)*0x50]
            action = struct.unpack_from('<Q', raw, 0x20)[0]
            if not action:
                continue
            action_name = s.reflection.header(action)['name']
            key = s.reflection.name(*struct.unpack_from('<II', raw, 0x28))
            original = raw[0x28:0x30]
            # Separate the native bow task's movement-stick steering from our
            # camera-relative pending movement. Keep the bow task fully running.
            wanted = {'IA_OpenUI_MultiplayerWheel':'S', 'IA_Gamepad_Move':'Gamepad_Left2D'}
            if action_name not in wanted:
                continue
            expected = wanted[action_name]
            if key == expected:
                # Runtime FKey caches can be populated while source caches
                # remain empty. Remember both for inherited disabled rows.
                self.source_keys[(action, raw[0x30:0x40])] = original
            if key == 'None':
                original = self.source_keys.get((action, raw[0x30:0x40]))
            if (key != expected and not (key=='None' and original)) or original is None:
                continue
            token = (owner, descriptor, row, raw[:0x28], raw[0x30:0x40])
            if token not in self.bindings:
                self.bindings[token] = DisabledKey(self, owner, header, descriptor, row, original, raw)

    def collect(self):
        for source in self.sources:
            self.capture(source, 'InputMappingContext', 'Mappings', source=True)
        obj = self.session.pointer(self.session.pc, 'PlayerController', 'PlayerInput')
        if obj and self.session.is_a(self.session.reflection.header(obj)['class'], 'EnhancedPlayerInput'):
            self.capture(obj, 'EnhancedPlayerInput', 'EnhancedActionMappings')

    def set_input(self, axes, eligible=True):
        movement_vector(0., axes)  # Reject invalid input before changing state.
        self.axes, self.eligible = axes, bool(eligible)

    def clear_pending(self):
        if self.pending is None:
            return
        self.validate()
        # The native frame may already have consumed it or replaced it with
        # controller input. Never replay an old movement snapshot on handoff.
        if self.game.read(self.address, 24) == self.pending:
            self.game.write(self.address, ZERO)
        self.pending = None

    def update(self):
        self.validate()
        now = time.monotonic()
        if now >= self.next_collect or any(not r.valid() for r in self.bindings.values()):
            try:
                self.collect()
            except MappingRebuilt:
                self.clear_pending()
                return False
            self.bindings = {k:r for k,r in self.bindings.items() if r.valid()}
            self.next_collect = now+.04
        self.applied = True
        for record in self.bindings.values():
            record.disable()
        # Native IgnoreMoveInput includes menus, cinematics and ability locks.
        # Read only: never clear the game's lock counter.
        if not self.eligible or self.axes == (0., 0.) or self.game.read(self.session.pc+0x339, 1) != b'\0':
            self.clear_pending()
            return
        self.pending = movement_vector(self.session.yaw, self.axes)
        self.game.write(self.address, self.pending)
        return True

    def restore(self):
        self.restore_failures.clear()
        self.eligible, self.axes = False, (0., 0.)
        try:
            self.clear_pending()
            if self.applied:
                self.collect()  # Include inherited copies rebuilt this frame.
        except (OSError, RuntimeError, ValueError):
            self.restore_failures.add('keyboard input handoff')
        for token, record in self.bindings.items():
            try:
                if not record.restore():
                    self.restore_failures.add(token)
            except (OSError, RuntimeError, ValueError):
                self.restore_failures.add(token)
        if not self.restore_failures:
            self.applied = False
            self.next_collect = 0.
