"""Hide local first-person attachments and user-selected armor slots.

No shared asset, actor, inventory, replication or collision state is changed.
Only non-replicated, non-colliding mesh components beneath the local mesh qualify.
"""
from native_layout import matches as native_matches, rva as native_rva

import time

class EquipmentVisibility:
    def __init__(self, profile, obj, parent, socket):
        self.profile, self.obj = profile, obj
        s, g = profile.session, profile.game
        self.identity = s.identity(obj)
        self.parent, self.socket = parent, socket
        hidden = s.field(obj, 'SceneComponent', 'bHiddenInGame')
        self.hidden, self.hidden_mask = hidden['address'], hidden['bool']['byteMask']
        self.original = g.read(self.hidden, 1)[0] & self.hidden_mask
        self.replicated = s.field(obj, 'ActorComponent', 'bReplicates')
        body = s.field(obj, 'PrimitiveComponent', 'BodyInstance')['address']
        self.collision = body+profile.collision_offset
        self.simulation = body+profile.simulation_offset
        self.simulation_mask = profile.simulation_mask
        self.registered, self.dirty = obj+0x94, obj+0x95
        self.applied = False
        if not self.eligible() or g.read(self.registered, 1)[0] & 3 != 3:
            raise RuntimeError('Equipment is replicated, colliding, simulated or not render-ready')

    def owned(self):
        s = self.profile.session
        try:
            return (s.identity(self.obj) == self.identity and
                    s.pointer(self.obj, 'SceneComponent', 'AttachParent') == self.parent and
                    s.value(self.obj, 'SceneComponent', 'AttachSocketName', '<II') == self.socket)
        except (OSError, RuntimeError, ValueError):
            return False

    def eligible(self):
        g = self.profile.game
        return (not g.read(self.replicated['address'], 1)[0] & self.replicated['bool']['byteMask']
                and g.read(self.collision, 1) == b'\0'
                and not g.read(self.simulation, 1)[0] & self.simulation_mask)

    def set_hidden(self, bits):
        g = self.profile.game
        value = g.read(self.hidden, 1)[0]
        if value & self.hidden_mask != bits:
            g.write(self.hidden, bytes([(value & ~self.hidden_mask) | bits]))
        # Measured MarkRenderStateDirty flag. The animated attachment's normal
        # end-of-frame update consumes it; no render queue or native call is made.
        value = g.read(self.dirty, 1)[0]
        if not value & 1:
            g.write(self.dirty, bytes([value | 1]))

    def update(self):
        if not self.owned():
            return False
        if not self.eligible():
            if not self.restore():
                raise RuntimeError('Could not restore equipment that changed mode')
            return False
        g = self.profile.game
        if not self.applied or g.read(self.hidden, 1)[0] & self.hidden_mask != self.hidden_mask:
            self.applied = True
            self.set_hidden(self.hidden_mask)
        return True

    def restore(self):
        if not self.applied or not self.owned():
            return True
        self.set_hidden(self.original)
        restored = self.profile.game.read(self.hidden, 1)[0] & self.hidden_mask == self.original
        if restored:
            self.applied = False
        return restored


class EquipmentProfile:
    def __init__(self, avatar, *, hide_body=True):
        self.avatar = avatar
        self.session, self.game = avatar.session, avatar.game
        self.records, self.skipped = {}, set()
        self.hide_body = bool(hide_body)
        self.hidden_slots = frozenset(getattr(self.session, 'hidden_armor_slots', ()))
        self.slot_reader = None
        self.desired = set()
        if self.hidden_slots:
            from armor_slots import ArmorSlots
            self.slot_reader = ArmorSlots(self.session)
        if not native_matches(self.game, 0x3B39C31, bytes.fromhex('808e9500000001')):
            raise RuntimeError('Render-state flag signature changed')
        self.restore_failures = set()
        self.next_scan = 0.
        s = self.session
        for name in ('ActorComponent', 'PrimitiveComponent', 'BodyInstance', 'BodyInstanceCore', 'SkeletalMesh',
                     'Skeleton', 'SkeletalMeshSocket', 'StaticMeshComponent'):
            s.ensure_layout(name)
        collision = next(p for p in s.layouts['BodyInstance']['properties'] if p['name'] == 'CollisionEnabled')
        simulation = next(p for p in s.layouts['BodyInstanceCore']['properties'] if p['name'] == 'bSimulatePhysics')
        if collision['size'] != 1 or simulation['bool']['fieldSize'] != 1:
            raise RuntimeError('Unexpected equipment physics layout')
        self.collision_offset = collision['offset']
        self.simulation_offset, self.simulation_mask = simulation['offset'], simulation['bool']['byteMask']
        from avatar_profile import BONES, HIDDEN
        self.hidden_bones = {BONES[i][0] for i in HIDDEN}
        self.socket_bones = {name: name for name, _ in BONES}
        skeleton = s.pointer(avatar.asset, 'SkeletalMesh', 'Skeleton')
        # Skeleton sockets first; mesh sockets take precedence, as in Unreal.
        for obj, cls in ((skeleton, 'Skeleton'), (avatar.asset, 'SkeletalMesh')):
            ptr, count, cap = s.value(obj, cls, 'Sockets', '<Qii')
            if not 0 <= count <= cap <= 256:
                raise RuntimeError('Unexpected equipment socket table')
            for i in range(count):
                sock = self.game.u64(ptr+i*8)
                name = s.reflection.name(*s.value(sock, 'SkeletalMeshSocket', 'SocketName', '<II'))
                bone = s.reflection.name(*s.value(sock, 'SkeletalMeshSocket', 'BoneName', '<II'))
                self.socket_bones[name] = bone
        self.collect()

    def set_hidden_slots(self, slots):
        from armor_slots import ArmorSlots, SLOTS
        slots = frozenset(slots)
        if not slots <= SLOTS:
            raise ValueError('Unknown armor visibility slot')
        if slots == self.hidden_slots:
            return
        if slots and self.slot_reader is None:
            self.slot_reader = ArmorSlots(self.session)
        self.hidden_slots = slots
        self.next_scan = 0.

    def collect(self):
        s, g = self.session, self.game
        armor = self.slot_reader.collect(self.hidden_slots) if self.slot_reader is not None else {}
        desired = set()
        queue, seen = [(self.avatar.mesh, None, 0)], set()
        while queue:
            parent, inherited_bone, depth = queue.pop(0)
            if parent in seen or depth > 12:
                continue
            seen.add(parent)
            if len(seen) > 200:
                raise RuntimeError('Unexpectedly large local attachment tree')
            ptr, count, cap = s.value(parent, 'SceneComponent', 'AttachChildren', '<Qii')
            if not 0 <= count <= cap <= 256:
                raise RuntimeError('Invalid local equipment attachment array')
            for i in range(count):
                obj = g.u64(ptr+i*8)
                if not obj:
                    continue
                s.reflection.headers.pop(obj, None)
                try:
                    h = s.reflection.header(obj)
                    if h['flags'] & 0x30 or s.pointer(obj, 'SceneComponent', 'AttachParent') != parent:
                        continue
                    socket = s.value(obj, 'SceneComponent', 'AttachSocketName', '<II')
                    name = s.reflection.name(*socket)
                    bone = self.socket_bones.get(name, inherited_bone)
                    queue.append((obj, bone, depth+1))
                    mesh = s.is_a(h['class'], 'StaticMeshComponent') or s.is_a(h['class'], 'SkinnedMeshComponent')
                    if not mesh or not ((self.hide_body and bone in self.hidden_bones) or obj in armor):
                        continue
                    desired.add(obj)
                    old = self.records.get(obj)
                    if old is not None and old.owned():
                        continue
                    self.records[obj] = EquipmentVisibility(self, obj, parent, socket)
                except (OSError, RuntimeError, ValueError) as error:
                    if obj not in self.skipped:
                        self.skipped.add(obj)
                        print(f'Equipment component skipped: {error}', flush=True)
        self.desired = desired

    def update(self, *, hide_body=True):
        if self.hide_body != bool(hide_body):
            self.hide_body = bool(hide_body)
            self.next_scan = 0.
        now = time.monotonic()
        if now >= self.next_scan:
            self.collect()
            self.next_scan = now+.15
        for obj, record in list(self.records.items()):
            if obj not in self.desired:
                if not record.restore():
                    raise RuntimeError('Could not restore deselected equipment visibility')
                self.records.pop(obj)
            elif not record.update():
                self.records.pop(obj)

    def restore(self):
        self.restore_failures.clear()
        for obj, record in self.records.items():
            try:
                if not record.restore():
                    self.restore_failures.add(obj)
            except (OSError, RuntimeError, ValueError):
                self.restore_failures.add(obj)
