"""Filter the local targeter's ranged eligibility; native scoring still runs.

Only the ranged bit in existing candidate rows changes. Item/melee eligibility,
array ownership, score data, target settings and ability timing remain native.
"""
import math
import struct

from inspect_camera import OBJECT_ARRAY_RVA


RANGED = 4
STRIDE = 0x38
NATIVE_GUARDS = {
    0x5BCDC89: bytes.fromhex('4c8da628030000'),  # Local candidate array +328.
    0x5BCDE10: bytes.fromhex('486bd838'),        # Candidate stride 38.
    0x5BCE5E0: bytes.fromhex('41f60704'),        # Ranged eligibility bit.
    0x637D31A: bytes.fromhex('488b89e00100004881c154010000'),
}


def in_front(origin, target, yaw, half_angle=75.):
    if not all(math.isfinite(v) for v in (*origin,*target,yaw,half_angle)):
        raise RuntimeError('Invalid ranged targeting position')
    dx,dy=target[0]-origin[0],target[1]-origin[1]
    length=math.hypot(dx,dy)
    if length < 1.:
        return True
    angle=math.radians(yaw)
    return (dx*math.cos(angle)+dy*math.sin(angle))/length >= math.cos(math.radians(half_angle))


class ForwardTargeting:
    def __init__(self, session, targeter):
        self.session,self.game,self.targeter=session,session.game,targeter
        self.disabled=set()
        self.restore_failures=set()
        for rva,expected in NATIVE_GUARDS.items():
            if self.game.read(self.game.base+rva,len(expected)) != expected:
                raise RuntimeError('Forward targeting native layout check failed')
        session.ensure_layout('MobCharacter')
        self.header=targeter+0x328

    def validate(self):
        s=self.session
        if s.identity(self.targeter) != s.identities[self.targeter] or \
                s.pointer(s.pc,'GameplayPlayerController','AutoTargeter') != self.targeter:
            raise RuntimeError('Forward targeting owner changed')

    def rows(self):
        self.validate()
        descriptor=self.game.read(self.header,16)
        pointer,count,capacity=struct.unpack('<Qii',descriptor)
        if not 0 <= count <= capacity <= 4096 or (count and not pointer):
            raise RuntimeError('Invalid forward targeting candidate array')
        data=self.game.read(pointer,count*STRIDE) if count else b''
        return descriptor,[(pointer+i*STRIDE,data[i*STRIDE],data[i*STRIDE+4:i*STRIDE+12],
                            data[i*STRIDE+0x30:(i+1)*STRIDE])
                           for i in range(count)]

    def weak_actor(self, weak):
        index,serial=struct.unpack('<ii',weak)
        if index < 0 or serial <= 0:
            return None
        g=self.game;array=g.base+OBJECT_ARRAY_RVA
        if index >= g.u32(array+0x24):
            return None
        chunks=g.u64(array+0x10)
        chunk=g.u64(chunks+(index//65536)*8)
        item=g.read(chunk+(index%65536)*24,24)
        if struct.unpack_from('<i',item,0x10)[0] != serial:
            return None
        actor=struct.unpack_from('<Q',item)[0]
        if not actor:
            return None
        # Do not let a reused cached reflection header misclassify a new actor.
        header=g.read(actor,0x28)
        flags,obj_index,cls=struct.unpack_from('<IIQ',header,8)
        if obj_index != index or flags & 0xC0000030 or not self.session.is_a(cls,'MobCharacter') \
                or self.session.is_a(cls,'PlayerCharacter'):
            return None
        return actor

    def position(self, actor):
        root=self.session.pointer(actor,'Actor','RootComponent')
        if not root:
            raise RuntimeError('Target actor has no root')
        return struct.unpack('<ddd',self.game.read(root+0x200,24))

    def write_flag(self, descriptor, address, weak, config, enabled):
        # Native overlap updates can resize/swap rows. Skip an unstable snapshot.
        if self.game.read(self.header,16) != descriptor or self.game.read(address+4,8) != weak \
                or self.game.read(address+0x30,8) != config:
            return False
        current=self.game.read(address,1)[0]
        desired=(current|RANGED) if enabled else (current&~RANGED)
        if current != desired:
            self.game.write(address,bytes([desired]))
        return True

    def update(self):
        descriptor,rows=self.rows()
        origin=struct.unpack('<ddd',self.game.read(self.session.root+0x200,24))
        in_front(origin,origin,self.session.yaw)  # Validate before the first write.
        present=set()
        for address,flags,weak,config in rows:
            key=(weak,config)
            present.add(key)
            if not flags&RANGED and key not in self.disabled:
                continue
            try:
                actor=self.weak_actor(weak)
                if actor is None:
                    continue  # Native eligibility handles objects outside the mob set.
                forward=in_front(origin,self.position(actor),self.session.yaw)
            except (OSError,RuntimeError,ValueError):
                continue  # A candidate can retire during the read-only snapshot.
            if forward:
                if key in self.disabled and self.write_flag(descriptor,address,weak,config,True):
                    self.disabled.discard(key)
            elif self.write_flag(descriptor,address,weak,config,False):
                self.disabled.add(key)
        if self.game.read(self.header,16) == descriptor:
            self.disabled.intersection_update(present)
        # Drop a previously selected rear mob while the native scorer catches
        # up. Never restore a stale weak reference when leaving first person.
        cached=self.game.read(self.targeter+0x154,8)
        try:
            actor=self.weak_actor(cached)
            rear=actor is not None and not in_front(origin,self.position(actor),self.session.yaw)
        except (OSError,RuntimeError,ValueError):
            rear=False
        if rear and self.game.read(self.targeter+0x154,8)==cached:
            self.game.write(self.targeter+0x154,bytes(8))

    def restore(self):
        self.restore_failures=set()
        if not self.disabled:
            return
        try:
            descriptor,rows=self.rows()
            for address,_,weak,config in rows:
                key=(weak,config)
                if key in self.disabled:
                    if not self.write_flag(descriptor,address,weak,config,True):
                        raise RuntimeError('Targeting array changed during restoration')
                    self.disabled.discard(key)
            if self.game.read(self.header,16) != descriptor:
                raise RuntimeError('Targeting array changed during restoration')
            self.disabled.clear()  # Remaining candidates have retired.
        except (OSError,RuntimeError,ValueError):
            self.restore_failures.add('ranged candidates')
