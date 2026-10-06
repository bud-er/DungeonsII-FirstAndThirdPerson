"""Suppress local mob occlusion flags and their mesh depth-highlighting pass."""
import time


class SilhouetteRecord:
    def __init__(self, profile, obj, identity):
        self.profile,self.obj,self.identity = profile,obj,identity
        s,g=profile.session,profile.game
        if s.identity(obj) != identity:
            raise RuntimeError('Mob identity changed before silhouette capture')
        p=s.field(obj,'BaseCharacter','EnableOcclusionSilhouetting')
        self.address,self.mask=p['address'],p['bool']['byteMask']
        self.original=g.read(self.address,1)[0]&self.mask
        self.applied=False
        self.operation='captured'

    def same_object(self):
        s=self.profile.session
        self.operation='object-slot retirement check'
        retired=getattr(s,'object_retired',None)
        if retired is not None and retired(self.obj,self.identity) is True:
            return False
        self.operation='actor identity check'
        return s.identity(self.obj)==self.identity

    def describe(self,action,error):
        return (f'Mob silhouette actor 0x{self.obj:X} {action}/{self.operation}: '
                f'{type(error).__name__}: {error}')

    def update(self):
        s,g=self.profile.session,self.profile.game
        # A dead mob can leave the shared catalogue before its freed UObject
        # header is readable. Prove slot retirement before any actor field read,
        # including when the silhouette bit was already cleared last frame.
        if not self.same_object():
            return False
        self.operation='silhouette flag read'
        value=g.read(self.address,1)[0]
        if value & self.mask:
            self.applied=True
            self.operation='silhouette flag write'
            g.write(self.address,bytes([value&~self.mask]))
        return True

    def restore(self):
        if not self.applied:
            return True
        s,g=self.profile.session,self.profile.game
        if not self.same_object():
            self.applied=False
            return True
        # Failed reads of an unchanged slot/header are not proof of retirement.
        # Leave the applied record intact for a retry and an explicit blocker.
        self.operation='silhouette flag read'
        value=g.read(self.address,1)[0]
        self.operation='silhouette flag write'
        g.write(self.address,bytes([(value&~self.mask)|self.original]))
        self.operation='silhouette flag read-back'
        if g.read(self.address,1)[0]&self.mask != self.original:
            return False
        self.applied=False
        return True


class SilhouetteProfile:
    def __init__(self, session, scene):
        self.session,self.game,self.scene=session,session.game,scene
        self.records,self.restore_failures={},set()
        self.restore_failure_details={}
        self.applied=False
        self.next_update=0.
        session.ensure_layout('BaseCharacter')
        p=next(p for p in session.layouts['BaseCharacter']['properties']
               if p['name']=='EnableOcclusionSilhouetting')
        if p['type']!='BoolProperty' or p['size']!=1 or p['bool']['fieldSize']!=1 \
                or p['bool']['byteOffset']!=0 or p['bool']['byteMask']!=1 \
                or p.get('flags',0x20)&0x20:  # CPF_Net: require verified non-replication.
            raise RuntimeError('Unexpected or replicated mob-silhouette property')
        from mob_depth_profile import MobDepthProfile
        self.depth = MobDepthProfile(session, scene)

    def collect(self):
        s=self.session
        for obj,identity in tuple(self.scene.characters.items()):
            record=self.records.get(obj)
            if record is not None and record.identity==identity:
                continue
            if record is not None:
                # A catalogue revision alone is not proof that our old actor
                # allocation has retired. Preserve its baseline until handed
                # back or positively retired by its saved object slot.
                try:
                    if not record.restore():
                        raise RuntimeError('silhouette flag read-back mismatch')
                except (OSError,RuntimeError,ValueError) as error:
                    raise RuntimeError(record.describe('replacement restore',error)) from error
            # The shared catalogue excludes PlayerCharacter, CDOs and templates.
            # Recheck class/identity before capturing a writable field.
            try:
                s.reflection.headers.pop(obj,None)
                cls=s.reflection.header(obj)['class']
                if not s.is_a(cls,'MobCharacter') or s.is_a(cls,'PlayerCharacter'):
                    continue
                self.records[obj]=SilhouetteRecord(self,obj,identity)
            except (OSError,RuntimeError,ValueError):
                continue  # An actor may disappear between discovery and capture.

    def update(self):
        # Mesh traversal has its own small per-frame budget and must advance
        # every frame, including between the cheaper actor-flag refreshes.
        self.depth.update()
        now=time.monotonic()
        if now<self.next_update:
            return
        self.next_update=now+.04
        self.collect()
        self.applied=True
        for obj,record in list(self.records.items()):
            try:
                if self.scene.characters.get(obj)!=record.identity or not record.update():
                    # Catalogue absence can also mean a failed discovery read.
                    # A still-live actor must receive its original preference.
                    if not record.restore():
                        raise RuntimeError('silhouette flag read-back mismatch')
                    self.records.pop(obj)
            except (OSError,RuntimeError,ValueError) as error:
                raise RuntimeError(record.describe('update',error)) from error

    def restore(self):
        previous=self.restore_failure_details
        self.restore_failure_details={}
        self.restore_failures.clear()
        self.depth.restore()
        self.restore_failures.update(self.depth.restore_failures)
        for obj,record in self.records.items():
            try:
                if not record.restore():
                    self.restore_failures.add(obj)
                    self.restore_failure_details[obj]=record.describe('restore',RuntimeError('silhouette flag read-back mismatch'))
            except (OSError,RuntimeError,ValueError) as error:
                self.restore_failures.add(obj)
                self.restore_failure_details[obj]=record.describe('restore',error)
        for obj,detail in self.restore_failure_details.items():
            if previous.get(obj)!=detail:
                print(detail,flush=True)
        self.restore_failure_details.update(self.depth.restore_failure_details)
        if not self.restore_failures:
            self.applied=False
            self.next_update=0.
