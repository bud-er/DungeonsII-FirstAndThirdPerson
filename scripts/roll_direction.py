"""Choose the direction of a new native roll without turning the camera.

The pinned game's GA_Roll creates a world-space constant-force source, using
C_RollStrengthOverTime. Only that new source's force direction is changed.
The engine owns its storage, strength curve, duration, collision and removal.
"""
import math
import struct
import time

R3=0x80
KEYBOARD_DODGE=0x10000  # Helper-only bit; never injected into native input.
DODGE_BUTTONS=R3 | KEYBOARD_DODGE
CONSTANT_FORCE_VTABLE=0x983ff30
NATIVE_GUARDS={
    0x3B57461:bytes.fromhex('488d8f280d0000'),  # Movement.CurrentRootMotion.
    0x3D47F15:bytes.fromhex('488d7918'),        # Pending source array +18.
    0x3D3E136:bytes.fromhex('488d05f31db005'),  # Constant-force vtable.
    0x5A88148:bytes.fromhex('410f1196c0000000'),# Roll force vector +C0.
    0x5A881D5:bytes.fromhex('4989bed8000000'),  # Roll strength curve +D8.
}


class RollUnavailable(RuntimeError):
    """An optional roll dependency is unavailable; keep native actions."""


class RollStateRebuilt(RuntimeError):
    """A native source array changed during a bounded read-only snapshot."""


class _RollSourceReadError(RuntimeError):
    """An optional source read failed; movement validation remains separate."""


def stick_yaw(view,x,y):
    if not all(math.isfinite(v) for v in (view,x,y)) or max(abs(x),abs(y))>1:
        raise RuntimeError('Invalid dodge stick direction')
    offset=math.degrees(math.atan2(x,y)) if math.hypot(x,y)>.1 else 0.
    return (view+offset+180)%360-180


class RollDirection:
    def __init__(self,session,movement):
        self.session,self.game,self.movement=session,session.game,movement
        self.request=None
        self.seen=set()
        self.last_buttons=0
        for rva,data in NATIVE_GUARDS.items():
            if self.game.read(self.game.base+rva,len(data)) != data:
                raise RuntimeError('Native roll direction layout check failed')
        for name in ('RootMotionSourceGroup','RootMotionSource_ConstantForce'):
            session.ensure_layout(name)
        field=session.field(movement,'CharacterMovementComponent','CurrentRootMotion')
        layout=session.layouts['RootMotionSource_ConstantForce']
        props={p['name']:p for p in layout['properties']}
        if (field['size'],field.get('target'),layout['size'],props['Force']['offset'],
                props['Force']['size'],props['StrengthOverTime']['offset']) != \
                (72,'RootMotionSourceGroup',224,0xC0,24,0xD8):
            raise RuntimeError('Unsupported roll force layout')
        self.headers=(field['address']+8,field['address']+0x18)
        curves=[o['address'] for o in session.objects if o['name']=='C_RollStrengthOverTime'
                and not o['flags']&0x30 and session.is_a(o['class'],'CurveFloat')]
        if len(curves)!=1:
            raise RollUnavailable('Native roll strength curve is not loaded')
        self.curve=curves[0]
        # Assets can unload during ordinary abilities, including bow shots.
        # This read-only dependency must never join the camera's critical
        # player/component identities and switch first person off.
        try:
            self.curve_identity=session.identity(self.curve)
            if struct.unpack('<I',self.curve_identity[1])[0] != next(
                    o['serial'] for o in session.objects if o['address']==self.curve):
                raise RollUnavailable('Native roll strength curve retired during discovery')
        except (OSError,RuntimeError,ValueError) as error:
            raise RollUnavailable('Native roll strength curve is unavailable') from error
        self.validate()

    def validate(self):
        s=self.session
        if s.identity(self.movement)!=s.identities[self.movement]:
            raise RuntimeError('Roll movement changed')
        if s.pointer(s.pawn,'Character','CharacterMovement')!=self.movement:
            raise RuntimeError('Roll movement owner changed')
        try:
            if s.identity(self.curve)!=self.curve_identity:
                raise RollUnavailable('Native roll strength curve retired')
        except (OSError,RuntimeError,ValueError) as error:
            raise RollUnavailable('Native roll strength curve is unavailable') from error

    def read_source(self,address,size):
        try:
            return self.game.read(address,size)
        except (OSError,ValueError) as error:
            self.validate()  # A replaced movement owner is still a fatal error.
            raise _RollSourceReadError(str(error)) from error

    def snapshot_changed(self):
        # A failed source read alone cannot prove retirement. Recheck the
        # owning shared rows before classifying the failure as a rebuild.
        try:
            for header,descriptor,pointer,data in self.snapshot_arrays:
                if self.game.read(header,16)!=descriptor:
                    return True
                if data and self.game.read(pointer,len(data))!=data:
                    return True
        except (OSError,ValueError):
            pass
        return False

    def source_snapshot(self):
        self.snapshot_arrays=[]
        result=[]
        for header in self.headers:
            descriptor=self.read_source(header,16)
            pointer,count,capacity=struct.unpack('<Qii',descriptor)
            record=[header,descriptor,pointer,None]
            self.snapshot_arrays.append(record)
            if not 0<=count<=capacity<=256 or (count and not pointer):
                raise _RollSourceReadError('Invalid native roll source array')
            data=self.read_source(pointer,count*16) if count else b''
            record[3]=data
            for index in range(count):
                shared=data[index*16:(index+1)*16]
                source,control=struct.unpack('<QQ',shared)
                if not source or not control:
                    continue
                if struct.unpack('<Q',self.read_source(source,8))[0]!=self.game.base+CONSTANT_FORCE_VTABLE:
                    continue
                raw=self.read_source(source,224)
                if struct.unpack_from('<Q',raw,0xD8)[0]!=self.curve or raw[0x27]:
                    continue
                local_id=struct.unpack_from('<H',raw,0xA)[0]
                age,duration=struct.unpack_from('<f',raw,0x18)[0],struct.unpack_from('<f',raw,0x20)[0]
                if not local_id or not all(math.isfinite(v) for v in (age,duration)) \
                        or not 0<=age<duration<=2. or raw[0x24]&6:
                    continue
                result.append(dict(token=(source,control,local_id),source=source,age=age,
                                   header=header,descriptor=descriptor,row=pointer+index*16,
                                   shared=shared,raw=raw))
        self.validate()
        for header,descriptor,pointer,data in self.snapshot_arrays:
            if self.read_source(header,16)!=descriptor or (data and self.read_source(pointer,len(data))!=data):
                raise RollStateRebuilt('Native roll source arrays changed during snapshot')
        return result

    def sources(self):
        for attempt in range(2):
            self.validate()
            try:
                return self.source_snapshot()
            except RollStateRebuilt:
                if attempt:
                    raise
            except _RollSourceReadError as error:
                self.validate()
                changed=self.snapshot_changed()
                if attempt:
                    if changed:
                        raise RollStateRebuilt('Native roll source storage retired during read') from None
                    raise RollUnavailable(f'Native roll source reads remain unavailable: {error}') from error

    def current_source(self,row):
        # Retry reads only. No write has occurred while this candidate is
        # checked, and a changed owning row is never dereferenced again.
        for attempt in range(2):
            self.validate()
            try:
                if self.read_source(row['header'],16)!=row['descriptor'] or \
                        self.read_source(row['row'],16)!=row['shared']:
                    return None
                raw=self.read_source(row['source'],224)
                if raw[:0x14]!=row['raw'][:0x14] or raw[0x24]&6 or \
                        raw[0x27] or raw[0xD8:0xE0]!=row['raw'][0xD8:0xE0] or \
                        raw[0xC0:0xD8]!=row['raw'][0xC0:0xD8]:
                    return None
                age=struct.unpack_from('<f',raw,0x18)[0]
                if not math.isfinite(age) or not 0<=age<=.12:
                    return None
                self.validate()
                if self.read_source(row['header'],16)!=row['descriptor'] or \
                        self.read_source(row['row'],16)!=row['shared']:
                    return None
                return raw
            except _RollSourceReadError as error:
                if attempt:
                    raise RollUnavailable(f'Native roll source retired or unreadable before steering: {error}') from error

    def set_input(self,buttons,left_stick,eligible=True,now=None):
        now=time.monotonic() if now is None else now
        if not eligible:
            self.request=None
        elif buttons&DODGE_BUTTONS and not self.last_buttons&DODGE_BUTTONS:
            yaw=stick_yaw(self.session.yaw,*left_stick)
            self.request=(now+.18,yaw,self.seen.copy())
        self.last_buttons=buttons

    def update(self,now=None):
        now=time.monotonic() if now is None else now
        if self.request is not None and now>self.request[0]:
            self.request=None
        try:
            sources=self.sources()
        except RollStateRebuilt:
            # Keep the previous seen set: a partial snapshot must not make an
            # old roll appear newly created on the next tick.
            return False
        if self.request is not None:
            deadline,yaw,existing=self.request
            if now>deadline:
                self.request=None
            else:
                for row in sources:
                    if row['token'] in existing or row['age']>.12:
                        continue  # Cooldown presses cannot redirect an existing roll.
                    force=struct.unpack_from('<ddd',row['raw'],0xC0)
                    strength=math.hypot(force[0],force[1])
                    if not all(math.isfinite(v) for v in force) or not 1<=strength<=50000:
                        continue
                    if self.current_source(row) is None:
                        continue
                    angle=math.radians(yaw)
                    value=struct.pack('<ddd',strength*math.cos(angle),strength*math.sin(angle),force[2])
                    # A failed write can be partial. Never retry it or restore
                    # an old force into a source which may already have ended.
                    self.request=None
                    try:
                        self.game.write(row['source']+0xC0,value)
                    except (OSError,ValueError) as error:
                        raise RuntimeError(f'Native roll force write failed; completion is uncertain and no retry was attempted: {error}') from error
                    print(f'Roll direction applied: {yaw:.1f} degrees.',flush=True)
                    break
        self.seen={row['token'] for row in sources}

    def clear(self):
        # This is the direction of a committed, short-lived native action, not
        # a persistent setting. Let that roll finish in its chosen direction.
        self.request=None
        self.seen.clear()
