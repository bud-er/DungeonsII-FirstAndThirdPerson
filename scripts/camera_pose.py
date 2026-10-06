"""Local camera pose override while the game's spring arm keeps tracking zones.

The spring arm's zoom/pose, zone snapshots and native FOV are read-only.
Only camera attachment membership changes. Socket/additive offsets below are
verified against native instructions in the pinned build.
"""
import math
import struct

from avatar_profile import multiply_quaternions
from camera_transition import slerp, unreal_rotator
from camera_attachment import CameraAttachment


def rotator_quaternion(pitch, yaw, roll=0.):
    p, y, r = (math.radians(v)/2 for v in (pitch, yaw, roll))
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return (cr*sp*sy-sr*cp*cy, -cr*sp*cy-sr*cp*sy,
            cr*cp*sy-sr*sp*cy, cr*cp*cy+sr*sp*sy)


def rotate(q, v):
    x, y, z, w = q
    a, b, c = v
    tx, ty, tz = 2*(y*c-z*b), 2*(z*a-x*c), 2*(x*b-y*a)
    return (a+w*tx+y*tz-z*ty, b+w*ty+z*tx-x*tz, c+w*tz+x*ty-y*tx)


def check_quaternion(q):
    if not all(math.isfinite(v) for v in q) or abs(sum(v*v for v in q)-1) > 1e-4:
        raise RuntimeError('Invalid camera transform quaternion')


def compose(parent_q, parent_pos, child_q, child_pos):
    check_quaternion(parent_q)
    check_quaternion(child_q)
    if not all(math.isfinite(v) and abs(v) < 1e9 for v in (*parent_pos, *child_pos)):
        raise RuntimeError('Invalid camera transform location')
    offset = rotate(parent_q, child_pos)
    return multiply_quaternions(parent_q, child_q), tuple(a+b for a,b in zip(parent_pos,offset))


class CameraPose:
    def __init__(self, session):
        self.session, self.game = session, session.game
        s, g = session, session.game
        for rva, expected in (
            (0x3D57632, '0f109120030000'),  # GetSocketTransform: quaternion +320.
            (0x3D57672, '66440f10b900030000'),  # Socket location +300.
            (0x3AF7954, 'f6838202000010'),  # GetCameraView: bUseAdditiveOffset.
            (0x3AF81B5, 'f30f1083f4020000'),  # Additive FOV +2F4.
        ):
            signature = bytes.fromhex(expected)
            if g.read(g.base+rva, len(signature)) != signature:
                raise RuntimeError('Camera pose layout signature changed')
        s.ensure_layout('ActorComponent')
        replicated = s.field(s.camera, 'ActorComponent', 'bReplicates')
        if g.read(replicated['address'],1)[0] & replicated['bool']['byteMask']:
            raise RuntimeError('Camera component is replicated')
        if s.pointer(s.camera, 'SceneComponent', 'AttachParent') != s.arm:
            raise RuntimeError('Unexpected camera parent')
        self.location = s.field(s.camera,'SceneComponent','RelativeLocation')['address']
        self.rotation = s.field(s.camera,'SceneComponent','RelativeRotation')['address']
        self.relative_pos = struct.unpack('<ddd',g.read(self.location,24))
        self.relative_q = rotator_quaternion(*struct.unpack('<ddd',g.read(self.rotation,24)))
        for name in ('bAbsoluteLocation','bAbsoluteRotation'):
            p = s.field(s.camera,'SceneComponent',name)
            if g.read(p['address'],1)[0] & p['bool']['byteMask']:
                raise RuntimeError('Native camera already uses absolute transforms')
            if name == 'bAbsoluteRotation':
                s.add_bool(s.camera,'SceneComponent',name,True)
        s.add_bool(s.camera,'CameraComponent','bUsePawnControlRotation',False)
        self.additive_flags, self.additive_fov = s.camera+0x282, s.camera+0x2F4
        additive = struct.unpack('<12d',g.read(s.camera+0x290,0x60))
        if g.read(self.additive_flags,1)[0]&0x10 or \
                any(abs(a-b)>1e-6 for a,b in zip(additive[:7]+additive[8:11],
                                                (0,0,0,1,0,0,0,1,1,1))):
            raise RuntimeError('Native camera is using an additive pose')
        self.saved_additive_fov = struct.unpack('<f',g.read(self.additive_fov,4))[0]
        if not math.isfinite(self.saved_additive_fov):
            raise RuntimeError('Invalid additive field of view')
        s.save(s.camera,self.additive_flags,1,0x10)
        s.plan.append((self.additive_flags,0x10,0x10))
        for address,size in ((self.location,24),(self.rotation,24),(self.additive_fov,4)):
            s.save(s.camera,address,size)
        self.native_fov_address = s.field(s.camera,'CameraComponent','FieldOfView')['address']
        self.native_pose()  # Validate before any writes.
        self.attachment = CameraAttachment(s)
        self.last_relative_pos = None
        self.last_blend = None

    def socket_pose(self):
        s, g = self.session, self.game
        transform = struct.unpack('<12d',g.read(s.arm+0x1E0,0x60))
        if any(abs(v-1)>1e-5 for v in transform[8:11]):
            raise RuntimeError('Unexpected spring-arm scale')
        socket = g.read(s.arm+0x300,0x40)
        return compose(transform[:4],transform[4:7],
                       struct.unpack_from('<dddd',socket,0x20),struct.unpack_from('<ddd',socket))

    def native_pose(self):
        q,pos = self.socket_pose()
        return compose(q,pos,self.relative_q,self.relative_pos)

    def write_world(self, q, pos=None):
        g, camera = self.game, self.session.camera
        g.write(camera+0x1E0,struct.pack('<dddd',*q))
        if pos is not None:
            g.write(camera+0x200,struct.pack('<ddd',*pos))

    def update(self, blend):
        s, g = self.session, self.game
        self.attachment.activate()
        root = struct.unpack('<12d',g.read(s.root+0x1E0,0x60))
        root_q,root_pos = root[:4],root[4:7]
        check_quaternion(root_q)
        if any(abs(v-1)>1e-5 for v in root[8:11]):
            raise RuntimeError('Unexpected player root scale')
        target_pos = (root_pos[0],root_pos[1],root_pos[2]+s.height)
        target_q = rotator_quaternion(s.pitch,s.yaw)
        if blend < 1:
            native_q,native_pos = self.native_pose()
            q = slerp(native_q,target_q,blend)
            pos = tuple(a+(b-a)*blend for a,b in zip(native_pos,target_pos))
        else:
            # At full first person the zoom arm is absent from the pose path.
            q,pos = target_q,target_pos
        if not all(math.isfinite(v) and abs(v)<1e9 for v in pos):
            raise RuntimeError('Invalid first-person camera position')
        native_fov = struct.unpack('<f',g.read(self.native_fov_address,4))[0]
        if not 5 <= native_fov <= 170:
            raise RuntimeError('Invalid native camera field of view')
        # Keep FieldOfView available to native zone logic; affect only the final view.
        g.write(self.additive_fov,struct.pack('<f',(s.fov-native_fov)*blend))
        inverse = (-root_q[0],-root_q[1],-root_q[2],root_q[3])
        relative_pos = rotate(inverse,tuple(a-b for a,b in zip(pos,root_pos)))
        position_changed = self.last_relative_pos is None or \
            any(abs(a-b)>1e-6 for a,b in zip(relative_pos,self.last_relative_pos))
        if position_changed:
            g.write(self.location,struct.pack('<ddd',*relative_pos))
        g.write(self.rotation,struct.pack('<ddd',*unreal_rotator(q)))
        # Once attached at eye height, let native parent propagation position
        # the camera on the same frame as the character. Do not rewind its
        # cached world position to an external sample while moving.
        self.write_world(q,pos if position_changed or blend < 1 or self.last_blend != 1 else None)
        self.last_relative_pos,self.last_blend = relative_pos,blend

    def restore_attachment(self):
        self.attachment.restore()

    def restore_world(self):
        s = self.session
        for obj in (s.camera,s.arm):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Camera parent changed during restoration')
        if s.pointer(s.camera,'SceneComponent','AttachParent') != s.arm:
            raise RuntimeError('Camera attachment changed during restoration')
        self.write_world(*self.native_pose())
