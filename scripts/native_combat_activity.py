"""Read this player's registered native combat abilities without changing them.

The camera must leave movement-facing control alone when activity is unavailable.
Possession changes remain fatal; optional ability churn is not a camera failure.
"""
import struct


class ActivityUnavailable(RuntimeError):
    pass


class _SnapshotChanged(ActivityUnavailable):
    pass


ATTACK_CLASSES = frozenset(('GA_PlayerMeleeAttack', 'GA_PlayerRangedAttack',
                            'GA_PlayerRangedAim', 'GA_HeavyJumpAttack',
                            'GA_Artifact', 'GA_HandleThrowable'))
REQUIRED_CLASSES = frozenset(('GA_PlayerMeleeAttack', 'GA_PlayerRangedAttack',
                              'GA_PlayerRangedAim', 'GA_Roll'))
TRACKED_CLASSES = ATTACK_CLASSES | {'GA_Roll'}


class NativeCombatActivity:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.scope = session.local_scope
        self.prototypes = {}
        self.instances = {}
        self.asc = self.asc_identity = None
        try:
            for name in ('BaseCharacter', 'AbilitySystemComponent', 'GameplayAbilitySpecContainer',
                         'GameplayAbilitySpec', 'GameplayAbility'):
                session.ensure_layout(name)
            self._check('AbilitySystemComponent', 'ActivatableAbilities', 'StructProperty', 0x400, 0x120,
                        'GameplayAbilitySpecContainer')
            self._check('GameplayAbilitySpecContainer', 'Items', 'ArrayProperty', 0x108, 16)
            self._check('GameplayAbilitySpecContainer', 'Owner', 'ObjectProperty', 0x118, 8,
                        'AbilitySystemComponent')
            self._check('GameplayAbilitySpec', 'Ability', 'ObjectProperty', 0x10, 8, 'GameplayAbility')
            self._check('GameplayAbilitySpec', 'ActiveCount', 'ByteProperty', 0x28, 1)
            self._check('GameplayAbilitySpec', 'NonReplicatedInstances', 'ArrayProperty', 0x78, 16)
            self._check('GameplayAbilitySpec', 'ReplicatedInstances', 'ArrayProperty', 0x88, 16)
            active = self._check('GameplayAbility', 'bIsActive', 'BoolProperty', 0x388, 1)
            if session.layouts['GameplayAbilitySpec']['size'] != 0xF0 or \
                    active['bool'] != dict(fieldSize=1, byteOffset=0, byteMask=1, fieldMask=255):
                raise ActivityUnavailable('Unsupported native ability activity layout')
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as error:
            raise ActivityUnavailable(f'Native ability layout unavailable: {error}') from error

    def _check(self, cls, name, kind, offset, size, target=None):
        prop = next(p for p in self.session.layouts[cls]['properties'] if p['name'] == name)
        if (prop['type'], prop['offset'], prop['size'], prop['dimensions']) != (kind, offset, size, 1) or \
                (target is not None and prop.get('target') != target):
            raise ActivityUnavailable(f'Unsupported native ability field: {cls}.{name}')
        return prop

    def _live(self, obj, expected):
        # This reads the saved slot/serial without first dereferencing the old
        # UObject. Instance headers are validated in the same bounded activity
        # read below; there is no reason to read its entire slot a second time.
        if self.session.object_retired(obj, expected) is not False:
            raise ActivityUnavailable('Native ability object retired')

    def _prototype(self, obj, handle):
        s = self.session
        key = handle, obj
        saved = self.prototypes.get(key)
        if saved is not None:
            # An unchanged registered spec holds its Ability CDO by a strong
            # object reference. Passive classifications need no per-frame
            # UObject scan. The unique spec handle prevents a replacement spec
            # inheriting this classification if a pointer is reused; the whole
            # spec buffer and owning ASC are rechecked before publishing.
            if not saved[1]:
                return saved[1]
            try:
                self._live(obj, saved[0])
            except (OSError, RuntimeError, ValueError):
                self.prototypes.pop(key, None)
                raise
            return saved[1]
        identity = s.identity(obj)
        s.reflection.headers.pop(obj, None)
        header = s.reflection.header(obj)
        if not header['flags'] & 0x10 or not s.is_a(header['class'], 'GameplayAbility'):
            raise ActivityUnavailable('Registered ability prototype is not a gameplay-ability CDO')
        kinds = frozenset(name for name in TRACKED_CLASSES if s.is_a(header['class'], name))
        self._live(obj, identity)
        self.prototypes[key] = identity, kinds
        return kinds

    @staticmethod
    def _array(raw, maximum):
        pointer, count, capacity = struct.unpack('<Qii', raw)
        if not 0 <= count <= capacity <= maximum or (count and not pointer):
            raise ActivityUnavailable('Invalid native ability array')
        return pointer, count

    @staticmethod
    def _coherence(data):
        # Prediction bookkeeping and input-pressed bits do not determine this
        # gate. Check handles, prototype pointers, active counts and instance
        # descriptors, rather than invalidating on unrelated spec updates.
        return tuple((data[i+0xC:i+0x18], data[i+0x28], data[i+0x78:i+0x98])
                     for i in range(0, len(data), 0xF0))

    def _instance_active(self, obj, kinds):
        s, g = self.session, self.game
        identity = self.instances.get(obj)
        if identity is None:
            identity = s.identity(obj)
        self._live(obj, identity)
        # One bounded read contains the UObject header and bIsActive. This also
        # validates owner/class before considering any instance's activity.
        raw = g.read(obj, 0x389)
        flags, index, cls = struct.unpack_from('<IIQ', raw, 8)
        outer = struct.unpack_from('<Q', raw, 0x20)[0]
        if flags & 0x30 or outer != s.pawn or not s.is_a(cls, 'GameplayAbility') or \
                not any(s.is_a(cls, name) for name in kinds):
            raise ActivityUnavailable('Registered ability instance is not owned by the local pawn')
        if raw[0xC:0x20] != identity[0]:
            raise ActivityUnavailable('Native ability instance changed during capture')
        if raw[0x388] not in (0, 1):
            raise ActivityUnavailable('Invalid native ability activity boolean')
        self.instances[obj] = identity
        return bool(raw[0x388])

    def snapshot(self):
        """Return (attack_active, roll_active), or conservatively unavailable.

        Passive/utility abilities are deliberately excluded. Coverage of the
        registered melee, ranged attack/aim and roll abilities is mandatory.
        """
        self.scope.validate()  # A changed local possession is a fatal error.
        s, g = self.session, self.game
        try:
            asc = s.pointer(s.pawn, 'BaseCharacter', 'AbilitySystemComponent')
            if not asc:
                raise ActivityUnavailable('Local ability system is temporarily absent')
            if asc != self.asc:
                self.asc, self.asc_identity = asc, s.identity(asc)
                self.prototypes.clear()
                self.instances.clear()
            self._live(asc, self.asc_identity)
            s.reflection.headers.pop(asc, None)
            header = s.reflection.header(asc)
            if header['outer'] != s.pawn or header['flags'] & 0x30 or \
                    not s.is_a(header['class'], 'AbilitySystemComponent'):
                raise ActivityUnavailable('Ability system is not owned by the local pawn')
            container = asc+0x400
            if g.u64(container+0x118) != asc:
                raise ActivityUnavailable('Native ability container ownership changed')
            descriptor = g.read(container+0x108, 16)
            pointer, count = self._array(descriptor, 512)
            data = g.read(pointer, count*0xF0) if count else b''
            attack_active = roll_active = False
            coverage, pointers, prototypes = set(), set(), set()
            for offset in range(0, len(data), 0xF0):
                row = data[offset:offset+0xF0]
                prototype = struct.unpack_from('<Q', row, 0x10)[0]
                if not prototype:
                    raise ActivityUnavailable('Registered ability prototype is temporarily absent')
                handle = struct.unpack_from('<I', row, 0xC)[0]
                if not handle or (handle, prototype) in prototypes:
                    raise ActivityUnavailable('Invalid or duplicate native ability spec handle')
                prototypes.add((handle, prototype))
                kinds = self._prototype(prototype, handle)
                if not kinds:
                    continue
                coverage.update(kinds)
                active = bool(row[0x28])
                for start in (0x78, 0x88):
                    ip, n = self._array(row[start:start+16], 64)
                    instance_data = g.read(ip, n*8) if n else b''
                    for (instance,) in struct.iter_unpack('<Q', instance_data):
                        if not instance:
                            raise ActivityUnavailable('Registered ability instance is temporarily absent')
                        pointers.add(instance)
                        active = self._instance_active(instance, kinds) or active
                    if n and g.read(ip, n*8) != instance_data:
                        raise _SnapshotChanged('Native ability instances changed during capture')
                if active:
                    attack_active = attack_active or bool(kinds & ATTACK_CLASSES)
                    roll_active = roll_active or 'GA_Roll' in kinds
            if not REQUIRED_CLASSES <= coverage:
                raise ActivityUnavailable('Required local combat abilities are not registered yet')
            # Recheck both buffers so a count/instance handoff cannot produce a
            # false idle result. Retired optional ability data never ends view.
            if g.read(container+0x108, 16) != descriptor or \
                    (count and self._coherence(g.read(pointer, count*0xF0)) != self._coherence(data)) or \
                    s.pointer(s.pawn, 'BaseCharacter', 'AbilitySystemComponent') != asc or \
                    g.u64(container+0x118) != asc:
                raise _SnapshotChanged('Native ability snapshot changed during capture')
            self.instances = {p: i for p, i in self.instances.items() if p in pointers}
            self.prototypes = {p: i for p, i in self.prototypes.items() if p in prototypes}
            result = bool(attack_active), bool(roll_active)
        except _SnapshotChanged as error:
            # A normal start/end handoff needs another coherent read, not a
            # costly full rediscovery of every registered passive prototype.
            raise ActivityUnavailable(f'Native combat activity unavailable: {error}') from error
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as error:
            # Retry fresh ownership/serial discovery after an optional object
            # retires, including a replacement reusing its predecessor's address.
            self.asc = self.asc_identity = None
            self.prototypes.clear()
            self.instances.clear()
            raise ActivityUnavailable(f'Native combat activity unavailable: {error}') from error
        self.scope.validate()
        return result
