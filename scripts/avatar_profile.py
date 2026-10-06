"""Local character presentation for the fingerprinted SK_Player_Master skeleton.

Only this pawn's mesh component is written. Shared mesh/material assets, the
actor root, controller rotation, movement, targeting and physics stay untouched.
Offsets below are measured from the pinned build, not portable UE defaults.
"""
import math
import struct


BONES = (
    ('J_Root', -1), ('J_Hip', 0), ('J_R_Leg', 1), ('J_R_Foot', 2),
    ('J_L_Leg', 1), ('J_L_Foot', 4), ('J_Spine', 1), ('J_Shoulders', 6),
    ('J_L_Arm', 7), ('J_L_Hand', 8), ('J_L_WeaponScale', 9), ('J_L_Weapon', 10),
    ('J_L_Weapon1Scale', 9), ('J_L_Weapon1', 12), ('J_L_Weapon2Scale', 9),
    ('J_L_Weapon2', 14), ('J_L_Weapon3Scale', 9), ('J_L_Weapon3', 16),
    ('J_R_Arm', 7), ('J_R_Hand', 18), ('J_R_Weapon', 19), ('J_Neck', 7),
    ('J_Head', 21), ('J_L_EyeIris', 21), ('J_L_EyePupil', 23),
    ('J_R_EyeIris', 21), ('J_R_EyePupil', 25), ('J_R_Eyebrow', 21),
    ('J_L_Eyebrow', 21), ('J_Mouth', 21), ('J_Socket', 0), ('J_SocketSword', 30),
)
HIDDEN = (1, 2, 3, 4, 5, 6, 7, *range(21, 30))


def hide_body(original):
    if len(original) != len(BONES) or any(v > 2 for v in original):
        raise RuntimeError('Unexpected bone visibility data')
    result = bytearray(original)
    for index in HIDDEN:
        result[index] = 2  # BVS_ExplicitlyHidden; preserve arm/weapon states.
    return bytes(result)


def yaw_quaternion(yaw):
    angle = math.radians(yaw) / 2
    return (0., 0., math.sin(angle), math.cos(angle))


def multiply_quaternions(a, b):
    x, y, z, w = a
    X, Y, Z, W = b
    return (w*X+x*W+y*Z-z*Y, w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W, w*W-x*X-y*Y-z*Z)


class AvatarProfile:
    def __init__(self, session, *, follow_view=True):
        self.session, self.game = session, session.game
        self.follow_view = follow_view
        self.restore_failures = set()
        self.applied = False
        for cls in ('Character', 'SkinnedMeshComponent', 'SkeletalMeshComponent', 'ActorComponent'):
            session.ensure_layout(cls)
        self.mesh = session.pointer(session.pawn, 'Character', 'Mesh')
        session.remember(self.mesh, 'SkeletalMeshComponent')
        self.asset = session.pointer(self.mesh, 'SkinnedMeshComponent', 'SkinnedAsset')
        session.remember(self.asset, 'SkeletalMesh')
        if session.reflection.header(self.asset)['name'] != 'SK_Player_Master':
            raise RuntimeError('Unsupported character mesh; use --no-avatar')
        replicated = session.field(self.mesh, 'ActorComponent', 'bReplicates')
        if self.game.read(replicated['address'], 1)[0] & replicated['bool']['byteMask']:
            raise RuntimeError('Refusing to rotate a replicated mesh component')
        ptr, count, capacity = struct.unpack('<Qii', self.game.read(self.asset+0x340, 16))
        if count != len(BONES) or not count <= capacity <= 256:
            raise RuntimeError('Unexpected player skeleton')
        bones = []
        for i in range(count):
            name, number, parent = struct.unpack('<IIi', self.game.read(ptr+i*12, 12))
            bones.append((session.reflection.name(name, number), parent))
        if tuple(bones) != BONES:
            raise RuntimeError('Player skeleton changed; avatar profile disabled')
        self.buffers = []
        for offset in (0x7A0, 0x7B0):
            header = self.game.read(self.mesh+offset, 16)
            pointer, count, capacity = struct.unpack('<Qii', header)
            if count != len(BONES) or not count <= capacity <= 256:
                raise RuntimeError('Unexpected mesh visibility buffers')
            saved = self.game.read(pointer, count)
            self.buffers.append((self.mesh+offset, header, pointer, saved, hide_body(saved)))
        self.rotation = session.field(self.mesh, 'SceneComponent', 'RelativeRotation')['address']
        self.saved_rotation = self.game.read(self.rotation, 24)
        pitch, self.yaw_offset, roll = struct.unpack('<ddd', self.saved_rotation)
        if abs(pitch) > 1e-5 or abs(roll) > 1e-5 or abs(self.yaw_offset+90) > 1e-5:
            raise RuntimeError('Unexpected player mesh orientation')
        absolute = session.field(self.mesh, 'SceneComponent', 'bAbsoluteRotation')
        self.absolute_address, self.absolute_mask = absolute['address'], absolute['bool']['byteMask']
        self.saved_absolute = self.game.read(self.absolute_address, 1)[0] & self.absolute_mask
        if self.saved_absolute:
            raise RuntimeError('Player mesh already uses absolute rotation')
        self.dirty = session.field(self.mesh, 'SkeletalMeshComponent', 'bRequiredBonesUpToDate')
        self.validate()
        from equipment_profile import EquipmentProfile
        shared = getattr(session, 'shared_equipment', None)
        if shared is not None:
            if shared.avatar.mesh != self.mesh or shared.avatar.asset != self.asset:
                raise RuntimeError('Shared armor presentation belongs to a different character')
            self.equipment = shared
        else:
            self.equipment = EquipmentProfile(self)

    def validate(self):
        s = self.session
        for obj in (self.mesh, self.asset, s.root):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Avatar object changed')
        if s.pointer(s.pawn, 'Character', 'Mesh') != self.mesh or \
                s.pointer(self.mesh, 'SkinnedMeshComponent', 'SkinnedAsset') != self.asset:
            raise RuntimeError('Character mesh changed')
        for address, header, *_ in self.buffers:
            if self.game.read(address, 16) != header:
                raise RuntimeError('Character visibility buffers changed')
        q = struct.unpack('<dddd', self.game.read(self.mesh+0x1E0, 32))
        if not all(math.isfinite(v) for v in q) or abs(sum(v*v for v in q)-1) > 1e-4:
            raise RuntimeError('Invalid mesh world transform')

    def set_absolute(self, value):
        current = self.game.read(self.absolute_address, 1)[0]
        self.game.write(self.absolute_address, bytes([(current & ~self.absolute_mask) | value]))

    def invalidate_bones(self):
        # This is a cache flag, not a preference: ask the engine to recalculate.
        address, mask = self.dirty['address'], self.dirty['bool']['byteMask']
        self.game.write(address, bytes([self.game.read(address, 1)[0] & ~mask]))

    def update(self):
        self.validate()
        first = not self.applied
        self.applied = True  # Even a partial write must run restoration.
        for _, _, pointer, _, hidden in self.buffers:
            self.game.write(pointer, hidden)
        if first:
            self.invalidate_bones()
        self.update_heading(validate=False)
        self.equipment.update()

    def update_heading(self, *, validate=True):
        if not self.applied:
            raise RuntimeError('Arm look refresh requires an active avatar profile')
        if validate:
            self.validate()
        if getattr(self, 'follow_view', True):
            yaw = (self.session.yaw + self.yaw_offset + 180) % 360 - 180
            self.set_absolute(self.absolute_mask)
            self.game.write(self.rotation, struct.pack('<ddd', 0, yaw, 0))
            self.game.write(self.mesh+0x1E0, struct.pack('<dddd', *yaw_quaternion(yaw)))

    def restore(self):
        self.restore_failures.clear()
        equipment = getattr(self, 'equipment', None)
        if equipment is not None:
            if getattr(equipment, 'shared', False) is True:
                # Camera exit reveals the body, retaining chosen armor visibility.
                try:
                    equipment.update(hide_body=False)
                except (OSError, RuntimeError, ValueError):
                    self.restore_failures.add('equipment')
            else:
                equipment.restore()
            if equipment.restore_failures:
                self.restore_failures.add('equipment')
        if not self.applied:
            return
        s = self.session
        try:
            if s.identity(self.mesh) != s.identities[self.mesh]:
                raise RuntimeError('Mesh identity changed')
        except (OSError, RuntimeError, ValueError):
            self.restore_failures.add('mesh identity')
            return
        # Restore each buffer independently; never write a replaced allocation.
        for address, header, pointer, original, _ in self.buffers:
            try:
                if self.game.read(address, 16) != header:
                    raise RuntimeError('Visibility allocation changed')
                self.game.write(pointer, original)
                if self.game.read(pointer, len(original)) != original:
                    raise RuntimeError('Visibility restoration did not stick')
            except (OSError, RuntimeError, ValueError):
                self.restore_failures.add(address)
        try:
            self.invalidate_bones()
            if not getattr(self, 'follow_view', True):
                if not self.restore_failures:
                    self.applied = False
                return
            self.game.write(self.rotation, self.saved_rotation)
            self.set_absolute(self.saved_absolute)
            # Use the current parent orientation: the player may have moved or
            # dodged since first person was enabled. Never restore an old yaw.
            if s.identity(s.root) != s.identities[s.root]:
                raise RuntimeError('Parent component changed')
            parent = struct.unpack('<dddd', self.game.read(s.root+0x1E0, 32))
            if not all(math.isfinite(v) for v in parent) or abs(sum(v*v for v in parent)-1) > 1e-4:
                raise RuntimeError('Invalid parent orientation')
            restored = multiply_quaternions(parent, yaw_quaternion(self.yaw_offset))
            self.game.write(self.mesh+0x1E0, struct.pack('<dddd', *restored))
            if self.game.read(self.rotation, 24) != self.saved_rotation or \
                    self.game.read(self.absolute_address, 1)[0] & self.absolute_mask != self.saved_absolute:
                raise RuntimeError('Mesh rotation restoration did not stick')
        except (OSError, RuntimeError, ValueError):
            self.restore_failures.add('mesh rotation')
        if not self.restore_failures:
            self.applied = False
