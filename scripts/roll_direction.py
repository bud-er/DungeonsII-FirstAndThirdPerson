"""Native keyboard dodge routing and the retained, inactive roll experiment.

KeyboardDodgeBindings maps third-person R to the existing pawn-forward action;
it changes no force, target data, code or cooldown. The legacy RollDirection
class below edits a force source and is not used by supported camera controls.
"""
from native_layout import matches as native_matches, rva as native_rva

import math
import struct
import time
from input_mapping_snapshot import mapping_snapshot, checked_row, MappingRebuilt

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


class _KeyboardDodgeAction:
    def __init__(self, profile, owner, header, descriptor, address, raw):
        self.profile, self.owner, self.header = profile, owner, header
        self.owner_id = profile.session.identity(owner)
        self.descriptor, self.address = descriptor, address
        raw = checked_row(profile.game, header, descriptor, address, raw)
        self.prefix, self.tail = raw[:0x20], raw[0x28:0x40]+raw[0x41:]
        self.trigger, self.trigger_id = profile.check_trigger(raw)
        self.applied = struct.unpack_from('<Q', raw, 0x20)[0] == profile.forward

    def valid(self):
        p, g, s = self.profile, self.profile.game, self.profile.session
        if s.identity(self.owner) != self.owner_id or g.read(self.header,16) != self.descriptor:
            return False
        raw = g.read(self.address, 0x50)
        return (all(s.identity(o) == i for o, i in p.action_ids.items()) and
                s.identity(self.trigger) == self.trigger_id and
                raw[:0x20] == self.prefix and raw[0x28:0x40]+raw[0x41:] == self.tail and
                g.u64(struct.unpack_from('<Q',self.prefix)[0]) == self.trigger and
                struct.unpack_from('<Q', raw, 0x20)[0] in (p.directional,p.forward) and
                g.read(self.header, 16) == self.descriptor)

    def obsolete(self):
        """Only proven retirement/rebinding permits dropping an owned row."""
        p, g, s = self.profile, self.profile.game, self.profile.session
        retired = s.object_retired(self.owner, self.owner_id)
        if retired is True:return True
        if retired is not False:raise RuntimeError('Keyboard dodge owner lifetime is unknown')
        if s.identity(self.owner) != self.owner_id:
            raise RuntimeError('Keyboard dodge owner changed during lifetime check')
        if g.read(self.header,16) != self.descriptor:return True
        try:action = g.u64(self.address+0x20)
        except OSError:
            if g.read(self.header,16) != self.descriptor:return True
            raise
        if g.read(self.header,16) != self.descriptor:return True
        return action not in (p.directional,p.forward)

    def apply(self, forward):
        if self.obsolete():return False
        if not self.valid():raise RuntimeError('Live keyboard dodge mapping metadata changed')
        value=struct.pack('<Q',self.profile.forward if forward else self.profile.directional)
        self.applied=True
        g=self.profile.game
        if g.read(self.address+0x20,8)!=value:g.write(self.address+0x20,value)
        if g.read(self.address+0x20,8)!=value:
            raise RuntimeError('Keyboard dodge action write did not persist')
        self.applied=bool(forward)
        return True

    def restore(self):
        if not self.applied:return True
        if self.obsolete():self.applied=False;return True
        if self.profile.game.u64(self.address+0x20)==self.profile.directional:
            self.applied=False;return True
        return self.apply(False)


class KeyboardDodgeBindings:
    """Third-person R uses native pawn-forward dodge after locomotion facing.

    R's normal cursor-directed action ignores the helper's movement vector.
    Only its action pointer changes; native press triggers, cooldown, server
    target commitment, root motion and all controller mappings remain native.
    This class never constructs or calls the legacy RollDirection force writer.
    """
    # GA_Roll's non-directional branch loads the pawn's RootComponent, then
    # obtains its forward vector. The alternative branch uses cursor/stick aim.
    FORWARD_GUARD = (0x5AAD5B3, bytes.fromhex('488b8fb80100004885c9740b488d55e7e8682b10fe'))

    def __init__(self,session):
        self.session,self.game=session,session.game
        self.records,self.restore_failures={},set()
        self.route_forward,self.applied=False,False
        self.next_collect=0.
        self.pc_identity=session.identity(session.pc)
        for name in ('InputAction','InputMappingContext','EnhancedPlayerInput','EnhancedActionKeyMapping'):
            session.ensure_layout(name)
        layout=session.layouts['EnhancedActionKeyMapping']
        fields={p['name']:p for p in layout['properties']}
        if layout['size']!=0x50 or any((fields[n]['offset'],fields[n]['size'])!=(offset,size)
                for n,offset,size in (('Action',0x20,8),('Key',0x28,24),('Triggers',0,16),('Modifiers',0x10,16))):
            raise RuntimeError('Unsupported keyboard dodge mapping layout')
        rva,expected=self.FORWARD_GUARD
        if not native_matches(self.game, rva, expected):
            raise RuntimeError('Native pawn-forward dodge check failed')
        sources=[o['address'] for o in session.objects if o['name']=='IMC_DefaultKBM' and
                 not o['flags']&0x30 and session.is_a(o['class'],'InputMappingContext')]
        if len(sources)!=1:raise RuntimeError('Expected one native keyboard dodge context')
        self.source=sources[0]
        self.source_identity=session.identity(self.source)
        _,_,rows=self.rows(self.source,'InputMappingContext','Mappings')
        directional=[r for r in rows if r['name']=='IA_DodgeDirectional' and r['key']=='R']
        forward=[r for r in rows if r['name']=='IA_DodgeForward' and r['key']=='ThumbMouseButton2']
        if len(directional)!=1 or len(forward)!=1:
            raise RuntimeError('Custom keyboard dodge mappings differ from the supported pair')
        self.directional,self.forward=directional[0]['action'],forward[0]['action']
        self.key=directional[0]['raw'][0x28:0x30]
        self.action_ids={o:session.identity(o) for o in (self.directional,self.forward)}
        for obj,expected in ((self.directional,2),(self.forward,0)):
            prop=session.field(obj,'InputAction','ValueType')
            if prop['size']!=1 or self.game.read(prop['address'],1)!=bytes([expected]):
                raise RuntimeError('Unsupported native keyboard dodge action value type')
        self.check_trigger(directional[0]['raw']);self.check_trigger(forward[0]['raw'])
        self.collect()

    def check_trigger(self,raw):
        pointer,count,capacity=struct.unpack_from('<Qii',raw)
        if count!=1 or not 1<=capacity<=16 or pointer<0x10000 or raw[0x10:0x20]!=bytes(16):
            raise RuntimeError('Unsupported keyboard dodge triggers or modifiers')
        trigger=self.game.u64(pointer)
        identity=self.session.identity(trigger)
        self.session.reflection.headers.pop(trigger,None)
        h=self.session.reflection.header(trigger)
        if h['name'].startswith('Default__') or not self.session.is_a(h['class'],'InputTriggerPressed'):
            raise RuntimeError('Keyboard dodge must use the native press trigger')
        if self.game.u64(pointer)!=trigger or self.session.identity(trigger)!=identity:
            raise MappingRebuilt('Keyboard dodge trigger rebuilt during capture')
        return trigger,identity

    def rows(self,owner,cls,name):
        s,g=self.session,self.game
        header=s.field(owner,cls,name)['address']
        descriptor,data=mapping_snapshot(g,header)
        pointer=struct.unpack_from('<Q',descriptor)[0]
        rows=[]
        for i in range(0,len(data),0x50):
            raw=data[i:i+0x50];action=struct.unpack_from('<Q',raw,0x20)[0]
            if action:rows.append(dict(address=pointer+i,raw=raw,action=action,
                name=s.reflection.header(action)['name'],key=s.reflection.name(*struct.unpack_from('<II',raw,0x28))))
        return header,descriptor,rows

    def capture(self,owner,cls,name):
        header,descriptor,rows=self.rows(owner,cls,name)
        for row in rows:
            raw=row['raw']
            if row['action'] not in (self.directional,self.forward) or raw[0x28:0x30]!=self.key:continue
            token=(owner,descriptor,row['address'],raw[:0x20],raw[0x28:0x40]+raw[0x41:])
            if token not in self.records:
                for old_token,old in list(self.records.items()):
                    if (old.owner,old.descriptor,old.address)!=(owner,descriptor,row['address']):continue
                    if old.applied and row['action']==self.forward:
                        raise RuntimeError('Owned keyboard dodge row metadata changed')
                    del self.records[old_token]
                self.records[token]=_KeyboardDodgeAction(self,owner,header,descriptor,row['address'],raw)
        return header,descriptor

    def collect(self):
        if self.session.object_retired(self.source,self.source_identity) is not False or \
                self.session.identity(self.source)!=self.source_identity:
            raise RuntimeError('Keyboard dodge source identity changed')
        self.capture(self.source,'InputMappingContext','Mappings')
        obj=self._runtime_owner()
        if obj and self.session.is_a(self.session.reflection.header(obj)['class'],'EnhancedPlayerInput'):
            self.capture(obj,'EnhancedPlayerInput','EnhancedActionMappings')

    def _runtime_owner(self):
        # Restoration can outlive possession, but must never follow a recycled
        # controller address into another player's input mappings.
        s=self.session
        retired=s.object_retired(s.pc,self.pc_identity)
        if retired is True:return 0
        if retired is not False or s.identity(s.pc)!=self.pc_identity:
            raise RuntimeError('Keyboard dodge controller lifetime is unknown')
        return s.pointer(s.pc,'PlayerController','PlayerInput')

    def update(self,*,enabled,key_down):
        # Never change the action underneath a held R during perspective,
        # focus or input-method handoffs. No synthetic press or delay is added.
        if key_down is False:self.route_forward=bool(enabled)
        if not self.route_forward:
            self.restore()
            if self.restore_failures:
                raise RuntimeError('Keyboard dodge restoration failed during perspective handoff')
            return True
        self.session.local_scope.validate()
        self.applied=True
        now=time.monotonic()
        if now>=self.next_collect or any(r.obsolete() for r in self.records.values()):
            try:self.collect()
            except MappingRebuilt:return False
            self.next_collect=now+.04
        self.records={k:r for k,r in self.records.items() if not r.obsolete()}
        for record in self.records.values():
            if not record.apply(self.route_forward):return False
        return True

    def restore(self):
        self.restore_failures=set()
        if not self.applied and not any(r.applied for r in self.records.values()):
            self.route_forward=False
            return
        # Restore the source before discovering the runtime copy: a native
        # rebuild during source restoration can inherit our forward action.
        # Bound retries and verify the array stayed stable after restoring it.
        for source in (True,False):
            for attempt in range(3):
                try:
                    owner=self.source if source else self._runtime_owner()
                    if source:
                        retired=self.session.object_retired(owner,self.source_identity)
                        if retired is True:owner=0
                        elif retired is not False:raise RuntimeError('Keyboard dodge source lifetime unknown')
                    cls,name=('InputMappingContext','Mappings') if source else ('EnhancedPlayerInput','EnhancedActionMappings')
                    identity=self.session.identity(owner) if owner else None
                    if owner and (source or self.session.is_a(self.session.reflection.header(owner)['class'],cls)):
                        header,descriptor=self.capture(owner,cls,name)
                    else:header=descriptor=None
                    for token,record in list(self.records.items()):
                        if (record.owner==self.source)!=source:continue
                        try:
                            if not record.restore():self.restore_failures.add(token)
                        except (OSError,RuntimeError,ValueError):self.restore_failures.add(token)
                    if (not source and self._runtime_owner()!=owner) or \
                            (owner and self.session.identity(owner)!=identity) or \
                            (header is not None and self.game.read(header,16)!=descriptor):
                        raise MappingRebuilt('Keyboard dodge context rebuilt during restoration')
                    break
                except MappingRebuilt:
                    if attempt==2:self.restore_failures.add('keyboard dodge context kept rebuilding')
                except (OSError,RuntimeError,ValueError,KeyError,StopIteration):
                    self.restore_failures.add('keyboard dodge context discovery')
                    break
        if not self.restore_failures:
            self.applied=False;self.route_forward=False;self.next_collect=0.


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
