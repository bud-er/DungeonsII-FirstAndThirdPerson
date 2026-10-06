"""Native KBM primary and force-attack actions share the mouse press.

The primary action stays intact. Alias its key to IA_RootPlayer so native KBM
combat attacks instead of starting mouse path-following. Shift temporarily
removes that alias for Shift+click interaction. Only key names change; native
action timing and cached FKey ownership stay intact.
The empty-ground release path is suppressed before aliases are applied. Shift
keeps native object interaction; menus and camera handoff restore the path.
"""
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


class MouseBindings:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.applied, self.restore_failures, self.interact = False, set(), False
        self.records, self.next_collect = {}, 0.
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
        self.collect()

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

    def set_input(self, interact):
        self.interact=bool(interact)

    def update(self):
        # Publish ownership before either profile can write. A rebuilt mapping
        # can defer the alias after suppression has already been established.
        self.applied=True
        if self.click_move_guard.update(interact=self.interact) is False:
            return False
        now=time.monotonic()
        if now>=self.next_collect or any(not r.valid() for r in self.records.values()):
            try:self.collect()
            except MappingRebuilt:return False
            self.next_collect=now+.04
            self.records={k:r for k,r in self.records.items() if r.valid()}
        for record in self.records.values():
            record.apply(self.interact)

    def restore(self):
        self.restore_failures.clear()
        if self.applied:
            try:self.collect()
            except (OSError,RuntimeError,ValueError,KeyError,StopIteration):
                self.restore_failures.add('mouse context discovery')
        for token,record in self.records.items():
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
            self.applied=False;self.next_collect=0.
        else:
            self.applied=True
