"""Suppress only mob mesh custom-depth highlights on this rendering client."""
from native_layout import matches as native_matches, rva as native_rva

import time


RENDER_DIRTY_GUARD = (0x3B39C31, bytes.fromhex('808e9500000001'))


class MobMeshDepth:
    def __init__(self, profile, actor, identity, obj, chain):
        self.profile, self.actor, self.actor_identity = profile, actor, identity
        self.obj, self.chain = obj, chain
        s, g = profile.session, profile.game
        self.identity = s.identity(obj)
        field = s.field(obj, 'PrimitiveComponent', 'bRenderCustomDepth')
        self.address, self.mask = field['address'], field['bool']['byteMask']
        self.original = g.read(self.address, 1)[0]&self.mask
        self.applied = False
        self.needs_refresh = False
        self.operation = 'captured'
        if not self.owned() or g.read(obj+0x94, 1)[0]&3 != 3:
            raise RuntimeError('Mob mesh is not owned or render-ready')

    def owned(self):
        s = self.profile.session
        try:
            # UObject slots remain readable after optional mob render objects
            # are freed. Check their saved serials before touching old headers.
            retired = getattr(s, 'object_retired', None)
            if retired is not None and (retired(self.obj, self.identity) is True or
                                        retired(self.actor, self.actor_identity) is True):
                return False
            if s.identity(self.actor) != self.actor_identity or s.identity(self.obj) != self.identity:
                return False
            # Every captured attachment link must still lead to this mob root.
            for obj, identity, parent in self.chain:
                if retired is not None and retired(obj, identity) is True:
                    return False
                if s.identity(obj) != identity:
                    return False
                if parent is None:
                    if s.pointer(self.actor, 'Actor', 'RootComponent') != obj:
                        return False
                elif s.pointer(obj, 'SceneComponent', 'AttachParent') != parent:
                    return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def set_depth(self, bits, *, refresh=True):
        g = self.profile.game
        self.operation = 'custom-depth flag read'
        value = g.read(self.address, 1)[0]
        if value&self.mask == bits and not self.needs_refresh:
            return
        # Animated meshes already schedule normal end-of-frame updates; no queue,
        # renderer pointer, visibility, collision or native call is changed.
        self.needs_refresh = True
        if value&self.mask != bits:
            self.operation = 'custom-depth flag write'
            g.write(self.address, bytes([(value&~self.mask)|bits]))
        if refresh:
            self.operation = 'render-dirty flag read'
            dirty = g.read(self.obj+0x95, 1)[0]
            self.operation = 'render-dirty flag write'
            g.write(self.obj+0x95, bytes([dirty|1]))
        self.needs_refresh = False

    def update(self):
        if not self.owned():
            return False
        # A native highlight refresh may enable the pass again.
        self.operation = 'custom-depth flag read'
        if self.profile.game.read(self.address, 1)[0]&self.mask or self.needs_refresh:
            self.applied = True
            self.set_depth(0)
        return True

    def restore(self):
        if not self.applied:
            return True
        # Restore our render preference on a live detached component too.
        # Attachment ownership restricts suppression, not this handoff.
        s = self.profile.session
        retired = getattr(s, 'object_retired', None)
        self.operation = 'object-slot retirement check'
        if retired is not None and retired(self.obj, self.identity) is True:
            self.applied = self.needs_refresh = False
            return True
        self.operation = 'mesh identity check'
        if s.identity(self.obj) != self.identity:
            self.applied = self.needs_refresh = False
            return True
        # Errors while reading a still-live slot/header are not retirement.
        # Keep the record applied so the caller can report/retry restoration.
        self.operation = 'render-state flag read'
        render_ready = self.profile.game.read(self.obj+0x94, 1)[0]&3 == 3
        # A live mesh may unregister temporarily. Restore its preference for
        # the next registration, but do not dirty an absent render state.
        self.set_depth(self.original, refresh=render_ready)
        self.operation = 'custom-depth flag read-back'
        if self.profile.game.read(self.address, 1)[0]&self.mask != self.original:
            return False
        self.applied = False
        return True

    def describe(self, action, error):
        return (f'Mob depth mesh 0x{self.obj:X} (actor 0x{self.actor:X}) '
                f'{action}/{self.operation}: {type(error).__name__}: {error}')


class MobDepthProfile:
    def __init__(self, session, scene):
        self.session, self.game, self.scene = session, session.game, scene
        self.records, self.restore_failures = {}, set()
        self.restore_failure_details = {}
        self.next_collect = 0.
        self.collecting = self.updating = None
        self.update_turn = False
        for name in ('Actor', 'SceneComponent', 'PrimitiveComponent', 'MeshComponent'):
            session.ensure_layout(name)
        field = next(p for p in session.layouts['PrimitiveComponent']['properties'] if p['name']=='bRenderCustomDepth')
        if (field['type'], field['offset'], field['size'], field['bool']['byteMask'],
            field['bool']['fieldSize'], field['bool']['byteOffset']) != ('BoolProperty', 0x271, 1, 0x40, 1, 0) \
                or field.get('flags', 0x20)&0x20:
            raise RuntimeError('Unsupported or replicated mob depth-render property')
        rva, expected = RENDER_DIRTY_GUARD
        if not native_matches(self.game, rva, expected):
            raise RuntimeError('Mob mesh render-state signature changed')

    def collect_steps(self):
        s, g = self.session, self.game
        for actor, identity in tuple(self.scene.characters.items()):
            yield
            try:
                s.reflection.headers.pop(actor, None)
                cls = s.reflection.header(actor)['class']
                if not s.is_a(cls, 'MobCharacter') or s.is_a(cls, 'PlayerCharacter') or s.identity(actor) != identity:
                    continue
                root = s.pointer(actor, 'Actor', 'RootComponent')
                queue, seen = [(root, (), None, 0)], set()
                while queue:
                    yield
                    obj, chain, parent, depth = queue.pop(0)
                    if not obj or obj in seen or depth > 12:
                        continue
                    seen.add(obj)
                    if len(seen) > 128:
                        raise RuntimeError('Unexpected mob attachment tree')
                    s.reflection.headers.pop(obj, None)
                    header = s.reflection.header(obj)
                    if header['flags']&0x30 or not s.is_a(header['class'], 'SceneComponent'):
                        continue
                    # Attached actors can be other players/mobs. Do not cross
                    # that ownership boundary even if their root is attached.
                    outer = header['outer']
                    for _ in range(8):
                        if outer == actor:
                            break
                        if not outer or s.is_a(s.reflection.header(outer)['class'], 'Actor'):
                            outer = None
                            break
                        outer = s.reflection.header(outer)['outer']
                    if outer != actor or (parent and s.pointer(obj, 'SceneComponent', 'AttachParent') != parent):
                        continue
                    chain = chain+((obj, s.identity(obj), parent),)
                    pointer, count, capacity = s.value(obj, 'SceneComponent', 'AttachChildren', '<Qii')
                    if not 0 <= count <= capacity <= 256 or (count and not pointer):
                        raise RuntimeError('Invalid mob attachment array')
                    children = [g.u64(pointer+i*8) for i in range(count)]
                    queue.extend((child, chain, obj, depth+1) for child in children)
                    # Decals, widgets, colliders and lock-on UI stay untouched.
                    if not s.is_a(header['class'], 'MeshComponent'):
                        continue
                    record = self.records.get(obj)
                    if record is not None:
                        if record.owned():
                            continue
                        # Recapture a changed attachment only after handing
                        # back the old preference. Never erase a failed restore.
                        if not record.restore():
                            continue
                    self.records[obj] = MobMeshDepth(self, actor, identity, obj, chain)
            except (OSError, RuntimeError, ValueError):
                continue  # Newly spawned/retired components settle next scan.

    def collect(self):
        # Explicit complete discovery is useful before a standalone restore;
        # the live camera loop uses the bounded iterator below.
        for _ in self.collect_steps():
            pass

    def update_steps(self):
        for obj, record in list(self.records.items()):
            # Discovery and updates interleave across ticks; a restored record
            # may have been replaced since this iterator took its snapshot.
            if self.records.get(obj) is not record:
                continue
            try:
                if not record.update():
                    if not record.restore():
                        raise RuntimeError('Detached mob mesh depth restoration failed')
                    if self.records.get(obj) is record:
                        self.records.pop(obj)
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(record.describe('update', error)) from error
            yield

    def update(self, budget=.0015):
        deadline = time.perf_counter()+budget
        if self.collecting is None and time.monotonic() >= self.next_collect:
            self.collecting = iter(self.collect_steps())
        # Alternate traversal and existing-record validation. A large level
        # cannot stall look updates or starve a native highlight refresh.
        for _ in range(128):
            self.update_turn = not self.update_turn
            if self.update_turn or self.collecting is None:
                if self.updating is None:
                    self.updating = iter(self.update_steps())
                try:
                    next(self.updating)
                except StopIteration:
                    self.updating = None
                    if self.collecting is None:
                        break
            else:
                try:
                    next(self.collecting)
                except StopIteration:
                    self.collecting = None
                    self.next_collect = time.monotonic()+.3
            if time.perf_counter() >= deadline:
                break

    def restore(self):
        self.collecting = self.updating = None
        previous = self.restore_failure_details
        self.restore_failure_details = {}
        self.restore_failures.clear()
        for obj, record in self.records.items():
            try:
                if not record.restore():
                    self.restore_failures.add(obj)
                    self.restore_failure_details[obj] = record.describe('restore', RuntimeError('custom-depth flag read-back mismatch'))
            except (OSError, RuntimeError, ValueError) as error:
                self.restore_failures.add(obj)
                self.restore_failure_details[obj] = record.describe('restore', error)
        for obj, detail in self.restore_failure_details.items():
            if previous.get(obj) != detail:
                print(detail, flush=True)
