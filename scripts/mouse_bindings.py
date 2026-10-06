"""Native KBM primary and force-attack actions share the mouse press.

On the ground, alias the primary key to IA_RootPlayer so native KBM combat
attacks instead of starting mouse path-following. Shift temporarily removes
that alias for Shift+click interaction. The airborne route changes only the
primary action pointer; native action timing and cached FKey ownership stay intact.
The empty-ground release path is suppressed before aliases are applied. Shift
keeps native object interaction; menus and camera handoff restore the path.
An airborne click uses the game's separate HeavyJumpAttack input action. It
still goes through the native ability checks; no ability is forced or patched.
"""
from native_layout import matches as native_matches, rva as native_rva

import struct
import time
from input_mapping_snapshot import mapping_snapshot, checked_row, MappingRebuilt
from click_move_guard import ClickMoveGuard


class KeyAlias:
    def __init__(self, profile, owner, header, descriptor, row, original, desired, expected=None):
        self.profile, self.owner, self.header = profile, owner, header
        self.identity = profile.session.identity(owner)
        self.descriptor, self.row = descriptor, row
        self.original, self.desired = original, desired
        raw = (profile.game.read(row, 0x50) if expected is None else
               checked_row(profile.game, header, descriptor, row, expected))
        self.prefix, self.tail = raw[:0x28], raw[0x30:0x40]+raw[0x41:]
        self.action = struct.unpack_from('<Q', raw, 0x20)[0]
        self.action_identity = profile.session.identity(self.action)
        self.applied = raw[0x28:0x30] in (desired, bytes(8))

    def valid(self):
        p, g = self.profile, self.profile.game
        try:
            raw = g.read(self.row, 0x50)
            return (p.session.identity(self.owner) == self.identity and
                    p.session.identity(self.action) == self.action_identity and
                    g.read(self.header, 16) == self.descriptor and
                    raw[:0x28] == self.prefix and raw[0x30:0x40]+raw[0x41:] == self.tail and
                    raw[0x28:0x30] in (self.original, self.desired, bytes(8)))
        except (OSError, RuntimeError, ValueError):
            return False

    def apply(self, interact=False):
        if not self.valid():
            return False
        self.applied = True
        value = bytes(8) if interact else self.desired
        if self.profile.game.read(self.row+0x28, 8) != value:
            self.profile.game.write(self.row+0x28, value)
        return True

    def restore(self):
        if not self.applied or not self.valid():
            return True
        self.profile.game.write(self.row+0x28, self.original)
        if self.profile.game.read(self.row+0x28, 8) != self.original:
            return False
        self.applied = False
        return True


class AirAction:
    """Retain the native LMB key/cache and change only its boolean action."""
    def __init__(self, profile, owner, header, descriptor, row, expected):
        self.profile, self.owner, self.header = profile, owner, header
        self.identity = profile.session.identity(owner)
        self.descriptor, self.row = descriptor, row
        raw = checked_row(profile.game, header, descriptor, row, expected)
        self.prefix, self.tail = raw[:0x20], raw[0x28:0x40]+raw[0x41:]
        self.applied = struct.unpack_from('<Q', raw, 0x20)[0] == profile.air_action

    def valid(self):
        p, g = self.profile, self.profile.game
        try:
            raw = g.read(self.row, 0x50)
            return (p.session.identity(self.owner) == self.identity and
                    p.session.identity(p.primary_action) == p.primary_identity and
                    p.session.identity(p.air_action) == p.air_identity and
                    g.read(self.header, 16) == self.descriptor and
                    raw[:0x20] == self.prefix and raw[0x28:0x40]+raw[0x41:] == self.tail and
                    struct.unpack_from('<Q', raw, 0x20)[0] in (p.primary_action, p.air_action))
        except (OSError, RuntimeError, ValueError):
            return False

    def apply(self, airborne):
        if not self.valid():
            return False
        p, g = self.profile, self.profile.game
        desired = struct.pack('<Q', p.air_action if airborne else p.primary_action)
        # Own the possible partial write before attempting it.
        self.applied = True
        if g.read(self.row+0x20, 8) != desired:
            g.write(self.row+0x20, desired)
        if g.read(self.row+0x20, 8) != desired:
            return False
        self.applied = bool(airborne)
        return True

    def restore(self):
        if not self.applied:
            return True
        p, g = self.profile, self.profile.game
        retired = p.session.object_retired(self.owner, self.identity)
        if retired is True:
            self.applied = False
            return True
        if retired is not False:
            return False
        if p.session.identity(self.owner) != self.identity:
            return False
        if g.read(self.header, 16) != self.descriptor:
            # Collection owns any inherited action in the replacement array.
            # The old allocation must never be touched after a rebuild.
            self.applied = False
            return True
        try:
            action = struct.unpack('<Q', g.read(self.row+0x20, 8))[0]
        except OSError:
            if g.read(self.header, 16) != self.descriptor:
                self.applied = False
                return True
            raise
        if g.read(self.header, 16) != self.descriptor:
            self.applied = False
            return True
        if action != p.air_action:
            # Already restored or explicitly rebound by the game/user. This
            # row no longer contains our action, regardless of other metadata.
            self.applied = False
            return True
        if not self.valid():
            # Same live array still contains our action but its key/cache or
            # action identity changed. Do not overwrite it or claim success.
            return False
        return self.apply(False)


class AirborneClickGate:
    """Use the validated local movement mode, never jump-key timing guesses."""
    def __init__(self, session):
        self.session, self.game = session, session.game
        session.ensure_layout('Character')
        session.ensure_layout('CharacterMovementComponent')
        self.movement = session.pointer(session.pawn, 'Character', 'CharacterMovement')
        session.remember(self.movement, 'PlayerCharacterMovementComponent')
        self.identity = session.identities[self.movement]
        prop = session.field(self.movement, 'CharacterMovementComponent', 'MovementMode')
        if (prop['type'], prop['offset'], prop['size'], prop['dimensions']) != ('ByteProperty', 0x231, 1, 1):
            raise RuntimeError('Unsupported airborne movement mode')
        self.address = prop['address']
        # The fingerprinted player's native speed getter explicitly branches
        # on MovementMode == MOVE_Falling (3). Read code only, never edit it.
        if not native_matches(self.game, 0x63B1F30, bytes.fromhex('80b93102000003')):
            raise RuntimeError('Unsupported native falling-mode check')

    def falling(self):
        s = self.session
        s.local_scope.validate()
        if s.identity(self.movement) != self.identity or \
                s.pointer(s.pawn, 'Character', 'CharacterMovement') != self.movement or \
                s.reflection.header(self.movement)['outer'] != s.pawn:
            raise RuntimeError('Airborne movement ownership changed')
        return self.game.read(self.address, 1) == b'\x03'


class MouseBindings:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.applied, self.restore_failures, self.interact = False, set(), False
        self.records, self.next_collect = {}, 0.
        self.air_records = {}
        self.air_available, self.air_gate, self.air_error = False, None, None
        self.primary_down = None
        self.route_air = False
        self.click_move_guard = ClickMoveGuard(session)
        for name in ('InputAction', 'InputMappingContext', 'EnhancedPlayerInput', 'EnhancedActionKeyMapping', 'Key'):
            session.ensure_layout(name)
        layout = session.layouts['EnhancedActionKeyMapping']
        props = {p['name']:p for p in layout['properties']}
        if layout['size'] != 0x50 or props['Action']['offset'] != 0x20 or \
                props['Action']['size'] != 8 or (props['Key']['offset'],props['Key']['size']) != (0x28,24):
            raise RuntimeError('Unsupported mouse action mapping layout')
        key_props = {p['name']:p for p in session.layouts['Key']['properties']}
        if session.layouts['Key']['size'] != 24 or \
                (key_props['KeyName']['offset'],key_props['KeyName']['size']) != (0,8):
            raise RuntimeError('Unsupported mouse key-name layout')
        self.sources = [obj['address'] for obj in session.objects if obj['name'] == 'IMC_DefaultKBM'
                        and not obj['flags'] & 0x30 and session.is_a(obj['class'], 'InputMappingContext')]
        if len(self.sources) != 1:
            raise RuntimeError('Expected one native keyboard/mouse context')
        rows = self.rows(self.sources[0], 'InputMappingContext', 'Mappings')[2]
        primary = [r for r in rows if r['action']=='IA_KBM_PrimaryAction' and r['key']=='LeftMouseButton']
        rooted = [r for r in rows if r['action']=='IA_RootPlayer' and r['key']=='LeftShift']
        if len(primary)!=1 or len(rooted)!=1:
            raise RuntimeError('Custom mouse/force-attack bindings differ from the supported pair')
        self.original, self.desired = rooted[0]['raw'][0x28:0x30], primary[0]['raw'][0x28:0x30]
        self.root_action = struct.unpack_from('<Q', rooted[0]['raw'], 0x20)[0]
        self.root_identity = session.identity(self.root_action)
        if session.reflection.name(0,0) != 'None':
            raise RuntimeError('Unsupported empty native key')
        self._configure_air(rows, primary[0])
        self.collect()

    def _configure_air(self, rows, primary):
        # Optional, read-only setup before any mapping changes. A customized
        # slam binding or an unavailable movement layout must not remove the
        # already validated ground controls. Runtime failures are NOT caught
        # here: once records can be changed, normal ownership/restore rules win.
        try:
            airborne = [r for r in rows if r['action']=='IA_HeavyJumpAttack' and r['key']=='Q']
            if len(airborne) != 1:
                raise RuntimeError('Expected one native HeavyJumpAttack Q mapping')
            self.primary_action = struct.unpack_from('<Q', primary['raw'], 0x20)[0]
            self.air_action = struct.unpack_from('<Q', airborne[0]['raw'], 0x20)[0]
            self.primary_identity, self.air_identity = (self.session.identity(a)
                for a in (self.primary_action, self.air_action))
            # Mapping-local modifiers/triggers cannot be transplanted between
            # actions. Native action-level triggers remain with their action.
            if any(row['raw'][:0x20] != bytes(0x20) for row in (primary, airborne[0])):
                raise RuntimeError('Unsupported primary/airborne input modifiers')
            for action in (self.primary_action, self.air_action):
                prop = self.session.field(action, 'InputAction', 'ValueType')
                if prop['size'] != 1 or self.game.read(prop['address'], 1) != b'\0':
                    raise RuntimeError('Primary and airborne actions must be native boolean inputs')
            self.air_gate = AirborneClickGate(self.session)
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as error:
            self.air_error = str(error)
            print(f'Airborne mouse routing unavailable: {error}. Ground mouse controls retained.', flush=True)
            return
        self.air_available = True

    def rows(self, owner, cls, name):
        s,g = self.session,self.game
        header = s.field(owner, cls, name)['address']
        descriptor, data = mapping_snapshot(g, header)
        pointer,count,capacity = struct.unpack('<Qii',descriptor)
        rows=[]
        for i in range(count):
            row=pointer+i*0x50;raw=data[i*0x50:(i+1)*0x50]
            action=struct.unpack_from('<Q',raw,0x20)[0]
            if action:
                rows.append(dict(address=row,raw=raw,action=s.reflection.header(action)['name'],
                                 key=s.reflection.name(*struct.unpack_from('<II',raw,0x28))))
        return header,descriptor,rows

    def capture(self, owner, cls, name):
        header,descriptor,rows = self.rows(owner,cls,name)
        if self.session.identity(self.root_action)!=self.root_identity:
            raise RuntimeError('Native force-attack action changed')
        for row in rows:
            raw=row['raw']
            if self.air_available and struct.unpack_from('<Q',raw,0x20)[0] in (self.primary_action,self.air_action) and \
                    raw[0x28:0x30] == self.desired and raw[:0x20] == bytes(0x20):
                token=(owner,descriptor,row['address'],raw[:0x20],raw[0x28:0x40]+raw[0x41:])
                if token not in self.air_records:
                    self.air_records[token]=AirAction(self,owner,header,descriptor,row['address'],raw)
            if struct.unpack_from('<Q',raw,0x20)[0]!=self.root_action or \
                    raw[0x28:0x30] not in (self.original,self.desired,bytes(8)):
                continue
            token=(owner,descriptor,row['address'],raw[:0x28],raw[0x30:0x40]+raw[0x41:])
            if token not in self.records:
                self.records[token]=KeyAlias(self,owner,header,descriptor,row['address'],self.original,self.desired,raw)

    def collect(self):
        for owner in self.sources:
            self.capture(owner,'InputMappingContext','Mappings')
        obj=self.session.pointer(self.session.pc,'PlayerController','PlayerInput')
        if obj and self.session.is_a(self.session.reflection.header(obj)['class'],'EnhancedPlayerInput'):
            self.capture(obj,'EnhancedPlayerInput','EnhancedActionMappings')

    def set_input(self, interact, *, primary_down=None):
        self.interact=bool(interact)
        self.primary_down=None if primary_down is None else bool(primary_down)

    def update(self):
        # Publish ownership before either profile can write. A rebuilt mapping
        # can defer the alias after suppression has already been established.
        self.applied=True
        if self.click_move_guard.update(interact=self.interact) is False:
            return False
        now=time.monotonic()
        if now>=self.next_collect or any(not r.valid() for r in (*self.records.values(),*self.air_records.values())):
            try:self.collect()
            except MappingRebuilt:return False
            self.next_collect=now+.04
            self.records={k:r for k,r in self.records.items() if r.valid()}
            self.air_records={k:r for k,r in self.air_records.items() if r.valid()}
        # Never turn a held ground attack into a new slam, or a held slam into
        # a fresh melee attack on landing. Changes wait for an observed release.
        if self.air_available and self.primary_down is False:
            self.route_air=self.air_gate.falling()
        if self.route_air:
            # RootPlayer is a ground force-attack command and can cancel/block
            # movement. Release that alias before routing to the slam action.
            for record in self.records.values():
                if not record.apply(True):return False
            for record in self.air_records.values():
                if not record.apply(True):return False
            return
        for record in self.air_records.values():
            if not record.apply(False):return False
        for record in self.records.values():
            if not record.apply(self.interact):return False

    def restore(self):
        self.restore_failures.clear()
        if self.applied:
            try:self.collect()
            except (OSError,RuntimeError,ValueError,KeyError,StopIteration):
                self.restore_failures.add('mouse context discovery')
        for token,record in (*self.air_records.items(),*self.records.items()):
            try:
                if not record.restore():self.restore_failures.add(token)
            except (OSError,RuntimeError,ValueError):self.restore_failures.add(token)
        # Even an alias setup/collection failure can leave the guard applied.
        # Restore native keys before permitting native click-path selection.
        try:
            self.click_move_guard.restore()
            if self.click_move_guard.restore_failures:
                self.restore_failures.add('native click path')
        except (OSError,RuntimeError,ValueError):
            self.restore_failures.add('native click path')
        if not self.restore_failures:
            self.applied=False;self.next_collect=0.;self.route_air=False;self.primary_down=None
        else:
            self.applied=True
