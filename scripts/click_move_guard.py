"""Suppress only the KBM empty-ground release path while first person owns input.

The native RootPlayer alias ends with the physical click. On release, the input
tree can otherwise choose PathTowardsCursor. Its native selection-enabled bit
provides an atomic data-only gate. Shift interactions use a different branch,
so they retain this guard too. No existing path is cancelled here.

The asset is shared inside this process: require one local player. Additional
components are allowed only on verified network controllers with no PlayerInput
and no active click-release branch. Losing eligibility still permits restoration
of the independently guarded asset.
"""
from native_layout import matches as native_matches, rva as native_rva

import struct
import time

from native_ranged_facing import array_descriptor, active_state_indices
from inspect_camera import OBJECT_ARRAY_RVA


MASK = 0x20
STATE_INDEX = 21
TREE_PACKAGE = '/Game/Spicewood/GameplayLogic/ST_PlayerInput'
NATIVE_GUARDS = {
    # State selector rejects disabled states before their conditions/tasks.
    0x551CE54: bytes.fromhex('f6465d200f84a8190000807e5b000f849e190000'),
    # GameplayPlayerController::AbortPathFollowing: zero status means idle.
    0x636EFB7: bytes.fromhex('488b89180900004885c9742480b94c02000000741b'),
    # The native StopPrimaryAction producer requires a validated input handler.
    0x63B42F2: bytes.fromhex('e8a98e010084c07452'),
    0x63B4312: bytes.fromhex('488b1d476d8105'),
    # A remote NetConnection cannot initialize the handler's LocalPlayer cache.
    0x63CD275: bytes.fromhex('488bcbe893fd2efb488bc8e83b24ddfd'),
    0x63CD2AE: bytes.fromhex('488d4f30e859282ffb84c00f840a020000'),
    0x63CD4C9: bytes.fromhex('32c0eb02'),
    # GetLocalPlayer reads Player and verifies the LocalPlayer class.
    0x419F6C6: bytes.fromhex('488d994803000048833b00741de89865d9ff488bd0488bcbe83d2f46fd84c07409'),
    0x419F6F0: bytes.fromhex('33c04883c4205bc3'),
}


class ClickMoveGuard:
    AUDIT_INTERVAL = .5
    AUDIT_SLICE = 256

    def __init__(self, session):
        self.session, self.game = session, session.game
        self.applied, self.restore_failures = False, set()
        self.ready = False
        self.tree = None
        self.next_audit = 0.
        self.audit_scan = None
        self.remotes = {}
        specs = {
            'PlayerStateTreeComponent': (0x130, [('StateTreeRef', 0x100, 40)]),
            'StateTree': (0x240, [('States', 0x50, 16), ('Nodes', 0x70, 16), ('Transitions', 0x60, 16)]),
            'CompactStateTransition': (0x30, [('RequiredEvent', 0, 16), ('State', 0x22, 2), ('Trigger', 0x28, 1)]),
            'CompactEventDesc': (0x10, [('Tag', 8, 8)]),
            'CompactStateTreeState': (0x60, [('Name', 0x10, 8), ('Parent', 0x2A, 2),
                ('ChildrenBegin', 0x2C, 2), ('ChildrenEnd', 0x2E, 2),
                ('EnterConditionsBegin', 0x30, 2), ('TasksBegin', 0x36, 2),
                ('EnterConditionsNum', 0x53, 1), ('TasksNum', 0x56, 1),
                ('SelectionBehavior', 0x5B, 1), ('bEnabled', 0x5D, 1)]),
            'StateTreeInstanceStorage': (0xE0, [('ExecutionState', 0x10, 0x78)]),
            'StateTreeExecutionState': (0x78, [('ActiveFrames', 0, 16)]),
            'StateTreeExecutionFrame': (0x68, [('StateTree', 0, 8), ('ActiveStates', 0xC, 0x34)]),
            'StateTreeActiveStates': (0x34, [('States', 0x20, 2), ('NumStates', 0x30, 1)]),
            'PlayerPathFollowingComponent': (0x320, []),
        }
        for name, (size, fields) in specs.items():
            session.ensure_layout(name)
            layout = session.layouts[name]
            props = {p['name']: p for p in layout['properties']}
            if layout['size'] != size or any((props.get(n, {}).get('offset'),
                    props.get(n, {}).get('size')) != (offset, length) for n, offset, length in fields):
                raise RuntimeError('Unsupported click path layout')
        enabled = next(p for p in session.layouts['CompactStateTreeState']['properties'] if p['name'] == 'bEnabled')
        if enabled.get('bool') != {'fieldSize': 1, 'byteOffset': 0, 'byteMask': MASK, 'fieldMask': MASK}:
            raise RuntimeError('Unsupported click path enabled bit')
        for rva, expected in NATIVE_GUARDS.items():
            if not native_matches(self.game, rva, expected):
                raise RuntimeError('Native click path instruction check failed')
        self.audit()

    def header(self, obj):
        self.session.reflection.headers.pop(obj, None)
        return self.session.reflection.header(obj)

    def audit_candidates(self, obj, players, components, trees):
        if obj is None:
            return
        s = self.session
        cls = obj['class']
        if not (s.is_a(cls, 'LocalPlayer') or s.is_a(cls, 'PlayerStateTreeComponent') or
                (obj['name'] == 'ST_PlayerInput' and s.is_a(cls, 'StateTree'))):
            return
        h = self.header(obj['address'])
        if h['flags'] & 0x30:
            return
        if s.is_a(h['class'], 'LocalPlayer'):
            players.append(h['address'])
        elif s.is_a(h['class'], 'PlayerStateTreeComponent'):
            if h['outer'] == s.pc and h['address'] != s.pointer(s.pc, 'GameplayPlayerController', 'StateTreeComponent'):
                raise RuntimeError('Multiple input consumers belong to the local controller')
            components.append(h['address'])
        elif h['name'] == 'ST_PlayerInput' and s.is_a(h['class'], 'StateTree'):
            trees.append(h['address'])
        if len(players) > 1 or len(components) > 8 or len(trees) > 1:
            raise RuntimeError('Click path guard found multiple local players or input-tree consumers')
        if s.is_a(h['class'], 'PlayerStateTreeComponent') and h['outer'] != s.pc:
            self.capture_remote(h)

    def audit(self):
        players, components, trees = [], [], []
        for obj in self.objects_iter():
            self.audit_candidates(obj, players, components, trees)
        self.finish_audit(players, components, trees)

    def objects_iter(self):
        """Read fresh headers from guarded slots, independent of header caches.

        Yield null slots too, so an audit slice bounds work on sparse arrays.
        A failed header read is ignorable only if its saved slot changed.
        """
        g = self.game
        array = g.base + native_rva(g, OBJECT_ARRAY_RVA)
        descriptor = g.read(array, 0x30)
        chunks = struct.unpack_from('<Q', descriptor, 0x10)[0]
        maximum, count, max_chunks, num_chunks = struct.unpack_from('<IIII', descriptor, 0x20)
        if (chunks < 0x10000 or not 0 < count <= maximum < 10_000_000 or
                not 0 < num_chunks <= max_chunks < 1000 or count > num_chunks * 65536):
            raise RuntimeError('Invalid click consumer object array')
        for first in range(0, count, self.AUDIT_SLICE):
            # Slice boundaries divide the native 65536-slot chunks exactly.
            chunk = g.u64(chunks + (first // 65536) * 8)
            if chunk < 0x10000:
                raise RuntimeError('Invalid click consumer object chunk')
            start = chunk + (first % 65536) * 24
            length = min(self.AUDIT_SLICE, count - first)
            slots = g.read(start, length * 24)
            if g.u64(array + 0x10) != chunks or g.u64(chunks + (first // 65536) * 8) != chunk:
                raise RuntimeError('Click consumer object array rebuilt')
            for i in range(length):
                item = slots[i * 24:(i + 1) * 24]
                obj = struct.unpack_from('<Q', item)[0]
                if not obj:
                    yield None
                    continue
                slot = start + i * 24
                try:
                    # Always read the class freshly, even for serial-zero
                    # objects. Unrelated objects need no FName/header lookup
                    # or second slot read; they cannot be input consumers.
                    cls = g.u64(obj + 0x10)
                    if not any(self.session.is_a(cls, name) for name in
                               ('LocalPlayer', 'PlayerStateTreeComponent', 'StateTree')):
                        yield None
                        continue
                    h = self.header(obj)
                except (OSError, RuntimeError, ValueError, UnicodeError):
                    current = g.read(slot, 24)
                    if current[:8] != item[:8] or current[0x10:0x14] != item[0x10:0x14]:
                        yield None
                        continue
                    raise
                current = g.read(slot, 24)
                if current[:8] != item[:8] or current[0x10:0x14] != item[0x10:0x14]:
                    yield None
                    continue
                if h['index'] != first + i:
                    raise RuntimeError('Click consumer object index changed without retirement')
                yield h

    def periodic_audit(self):
        # A global audit is needed for the shared asset, but a full synchronous
        # scan during camera ticks would stutter. Bound Python object work and
        # refresh candidate headers in slices; known ownership is checked every
        # update independently. New extra consumers reject immediately.
        if self.audit_scan is None:
            if time.monotonic() < self.next_audit:
                return
            self.audit_scan = iter(self.objects_iter())
            self.audit_lists = ([], [], [])
        for _ in range(self.AUDIT_SLICE):
            try:
                obj = next(self.audit_scan)
            except StopIteration:
                self.audit_scan = None
                self.finish_audit(*self.audit_lists)
                return
            self.audit_candidates(obj, *self.audit_lists)

    def finish_audit(self, players, components, trees):
        s = self.session
        player = s.pointer(s.pc, 'PlayerController', 'Player')
        component = s.pointer(s.pc, 'GameplayPlayerController', 'StateTreeComponent')
        if players != [player] or component not in components or len(trees) != 1:
            raise RuntimeError('Click path guard requires one local player and its input-tree consumer')
        h = self.header(component)
        if h['outer'] != s.pc or h['name'] != 'InputStateMachine':
            raise RuntimeError('Click input tree is not owned by the local controller')
        self.player, self.player_identity = player, s.identity(player)
        self.component, self.component_identity = component, s.identity(component)
        self.validate_local()
        tree = trees[0]
        if self.tree is None:
            self.capture_tree(tree)
        elif tree != self.tree or not self.asset_valid():
            raise RuntimeError('Click input tree asset changed')
        self.next_audit = time.monotonic() + self.AUDIT_INTERVAL

    def capture_remote(self, h):
        s = self.session
        component, owner = h['address'], h['outer']
        owner_header = self.header(owner)
        if (h['name'] != 'InputStateMachine' or owner == s.pc or
                not s.is_a(owner_header['class'], 'GameplayPlayerController') or owner_header['flags'] & 0x30 or
                s.pointer(owner, 'GameplayPlayerController', 'StateTreeComponent') != component):
            raise RuntimeError('Unknown additional click input consumer')
        player = s.pointer(owner, 'PlayerController', 'Player')
        if not player or not s.is_a(self.header(player)['class'], 'NetConnection'):
            raise RuntimeError('Additional click consumer is not a network controller')
        record = (owner, s.identity(owner), player, s.identity(player), s.identity(component))
        self.check_remote(component, record)
        self.remotes[component] = record

    def check_remote(self, component, record):
        s, g = self.session, self.game
        owner, owner_id, player, player_id, component_id = record
        for obj, identity in ((component, component_id), (owner, owner_id), (player, player_id)):
            if s.object_retired(obj, identity) is True or s.identity(obj) != identity:
                raise RuntimeError('Remote click consumer ownership changed')
        h = self.header(component)
        if (h['outer'] != owner or s.pointer(owner, 'GameplayPlayerController', 'StateTreeComponent') != component or
                s.pointer(owner, 'PlayerController', 'Player') != player or
                not s.is_a(self.header(player)['class'], 'NetConnection') or
                s.pointer(owner, 'PlayerController', 'PlayerInput')):
            raise RuntimeError('Remote click consumer gained local or ambiguous input')
        tree = g.u64(component + 0x100)
        shared = g.read(component + 0xD8, 16)
        storage, control = struct.unpack('<QQ', shared)
        if not tree and not storage and not control:
            return  # A freshly created, uninitialized remote component.
        if min(storage, control) < 0x10000:
            raise RuntimeError('Remote click input storage is incomplete')
        descriptor = g.read(storage + 0x10, 16)
        pointer, count = array_descriptor(descriptor, 8)
        if tree:
            th = self.header(tree)
            if (not s.is_a(th['class'], 'StateTree') or th['flags'] & 0x30 or
                    th['name'] not in ('ST_PlayerInput', 'ST_PlayerInput_Controller') or
                    self.header(th['outer'])['name'] != TREE_PACKAGE + ('_Controller' if th['name'].endswith('_Controller') else '')):
                raise RuntimeError('Remote controller uses an unknown input tree')
        elif count:
            raise RuntimeError('Remote input frames have no owning tree')
        frames = g.read(pointer, count * 0x68) if count else b''
        for i in range(count):
            frame = frames[i * 0x68:(i + 1) * 0x68]
            if struct.unpack_from('<Q', frame)[0] != tree:
                raise RuntimeError('Remote input tree is changing')
            active = active_state_indices(frame)
            if th['name'] == 'ST_PlayerInput' and set(active).intersection((14, 16, 17, 18, 19, 20, 21, 22)):
                raise RuntimeError('Remote controller has an active click-release branch')
        if (g.read(component + 0xD8, 16) != shared or g.u64(component + 0x100) != tree or
                g.read(storage + 0x10, 16) != descriptor or
                (frames and g.read(pointer, len(frames)) != frames) or
                s.identity(component) != component_id or s.identity(owner) != owner_id or
                s.identity(player) != player_id or s.pointer(owner, 'PlayerController', 'Player') != player or
                s.pointer(owner, 'PlayerController', 'PlayerInput')):
            raise RuntimeError('Remote click input ownership or execution changed')

    def validate_remotes(self):
        for component, record in list(self.remotes.items()):
            # Only proven retirement of the consumer itself removes a record.
            if self.session.object_retired(component, record[-1]) is True:
                del self.remotes[component]
                continue
            self.check_remote(component, record)

    def validate_local(self):
        s = self.session
        if (s.identity(s.pc) != s.identities[s.pc] or s.identity(s.pawn) != s.identities[s.pawn] or
                s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn or
                s.pointer(s.pawn, 'Pawn', 'Controller') != s.pc or
                s.pointer(s.pc, 'PlayerController', 'Player') != self.player or
                s.identity(self.player) != self.player_identity or
                s.pointer(s.pc, 'GameplayPlayerController', 'StateTreeComponent') != self.component or
                s.identity(self.component) != self.component_identity):
            raise RuntimeError('Local click input ownership changed')

    def capture_tree(self, tree):
        s, g = self.session, self.game
        h = self.header(tree)
        package = h['outer']
        if self.header(package)['name'] != TREE_PACKAGE:
            raise RuntimeError('Unexpected click input tree package')
        descriptor = g.read(tree + 0x50, 16)
        pointer, count = array_descriptor(descriptor, 128)
        if count != 49:
            raise RuntimeError('Unexpected click input state count')
        rows = g.read(pointer + 16 * 0x60, 7 * 0x60)
        expected = ((16, 'PrimaryActionRelease', 0, 77, 0, 21, 0),
                    (17, 'InteractionRelease', 16, 77, 1, 21, 1),
                    (18, 'Interaction', 17, 78, 0, 22, 0),
                    (19, 'MeleeAttackRelease', 16, 78, 1, 22, 1),
                    (20, 'Attack', 19, 79, 0, 23, 0),
                    (21, 'PathTowardsCursor', 16, 79, 3, 23, 2),
                    (22, 'Empty', 16, 82, 0, 25, 0))
        for index, name, parent, cond, cond_count, task, task_count in expected:
            row = rows[(index - 16) * 0x60:(index - 15) * 0x60]
            if (s.reflection.name(*struct.unpack_from('<II', row, 0x10)) != name or
                    struct.unpack_from('<H', row, 0x2A)[0] != parent or
                    struct.unpack_from('<H', row, 0x30)[0] != cond or row[0x53] != cond_count or
                    struct.unpack_from('<H', row, 0x36)[0] != task or row[0x56] != task_count):
                raise RuntimeError('Unsupported native click release branch')
        target = rows[5 * 0x60:6 * 0x60]
        fallback = rows[6 * 0x60:7 * 0x60]
        if not target[0x5D] & MASK or not target[0x5B] or not fallback[0x5D] & MASK or not fallback[0x5B]:
            raise RuntimeError('Native click release fallback is not enabled')
        if struct.unpack_from('<HH', rows, 0x2C) != (17, 23):
            raise RuntimeError('Unsupported native release child range')
        nodes_desc = g.read(tree + 0x70, 16)
        nodes, size, number = struct.unpack('<Qii', nodes_desc)
        if not 25 <= number <= 256 or not number * 16 <= size <= 65536 or nodes < 0x10000:
            raise RuntimeError('Invalid click input task container')
        task_type = g.u64(nodes + size - 24 * 16)
        if task_type != s.native['STT_PlayerPath']:
            raise RuntimeError('Click release task is not native PlayerPath')
        self.tree, self.tree_identity = tree, s.identity(tree)
        self.package, self.package_identity = package, s.identity(package)
        self.descriptor, self.rows, self.nodes_desc = descriptor, rows, nodes_desc
        self.row_start = pointer + 16 * 0x60
        self.address = pointer + STATE_INDEX * 0x60 + 0x5D
        self.original = target[0x5D] & MASK
        transitions_desc = g.read(tree + 0x60, 16)
        transitions_ptr, transitions_count = array_descriptor(transitions_desc, 256)
        transitions = g.read(transitions_ptr, transitions_count * 0x30)
        release = []
        for i in range(transitions_count):
            row = transitions[i * 0x30:(i + 1) * 0x30]
            if struct.unpack_from('<H', row, 0x22)[0] in (16, 21):
                if row[0x28] != 8 or s.reflection.name(*struct.unpack_from('<II', row, 8)) != 'SW.StateTree.Event.StopPrimaryAction':
                    raise RuntimeError('Click release has an unguarded event producer')
                release.append(i)
        if release != [4, 8, 21]:
            raise RuntimeError('Unsupported click release transition graph')
        self.transitions_desc, self.transitions = transitions_desc, transitions
        self.transitions_ptr = transitions_ptr
        if not self.asset_valid():
            raise RuntimeError('Click input asset changed during capture')

    @staticmethod
    def immutable_rows(raw):
        # Other bits in the same byte belong to the game and are preserved.
        raw = bytearray(raw)
        raw[5 * 0x60 + 0x5D] = 0
        return bytes(raw)

    def asset_valid(self):
        s, g = self.session, self.game
        if s.object_retired(self.tree, self.tree_identity) is True or s.identity(self.tree) != self.tree_identity:
            return False
        if (s.object_retired(self.package, self.package_identity) is True or
                s.identity(self.package) != self.package_identity):
            raise RuntimeError('Live click input tree package identity changed')
        if g.read(self.tree + 0x50, 16) != self.descriptor:
            # The new allocation may contain a copy of our disabled bit. A
            # live asset rebuild is not proof that restoration is complete.
            raise RuntimeError('Live click state allocation changed')
        if g.read(self.tree + 0x70, 16) != self.nodes_desc:
            raise RuntimeError('Live click task container changed')
        if (g.read(self.tree + 0x60, 16) != self.transitions_desc or
                g.read(self.transitions_ptr, len(self.transitions)) != self.transitions or
                g.read(self.tree + 0x60, 16) != self.transitions_desc):
            raise RuntimeError('Live click release transitions changed')
        rows = g.read(self.row_start, len(self.rows))
        if (s.object_retired(self.tree, self.tree_identity) is True or
                s.identity(self.tree) != self.tree_identity):
            return False
        if g.read(self.tree + 0x50, 16) != self.descriptor:
            raise RuntimeError('Live click state allocation changed during row read')
        if self.immutable_rows(rows) != self.immutable_rows(self.rows):
            raise RuntimeError('Live click release branch changed')
        return True

    def path_idle(self):
        s, g = self.session, self.game
        path = s.pointer(s.pc, 'GameplayPlayerController', 'PathFollowingComponent')
        h = self.header(path)
        if h['outer'] != s.pc or not s.is_a(h['class'], 'PlayerPathFollowingComponent') or h['flags'] & 0x30:
            raise RuntimeError('Click path component is not locally owned')
        identity = s.identity(path)
        before = g.read(path + 0x24C, 1)
        shared = g.read(self.component + 0xD8, 16)
        storage, control = struct.unpack('<QQ', shared)
        if min(storage, control) < 0x10000:
            return False
        descriptor = g.read(storage + 0x10, 16)
        pointer, count = array_descriptor(descriptor, 8)
        current_tree = g.u64(self.component + 0x100)
        tree_header = self.header(current_tree)
        if not s.is_a(tree_header['class'], 'StateTree') or tree_header['name'] not in ('ST_PlayerInput', 'ST_PlayerInput_Controller'):
            raise RuntimeError('Unexpected local click execution tree')
        frames = g.read(pointer, count * 0x68) if count else b''
        active = False
        for index in range(count):
            frame = frames[index * 0x68:(index + 1) * 0x68]
            tree = struct.unpack_from('<Q', frame)[0]
            if tree != current_tree:
                return False
            if tree == self.tree and STATE_INDEX in active_state_indices(frame):
                active = True
        self.validate_local()
        if (s.pointer(s.pc, 'GameplayPlayerController', 'PathFollowingComponent') != path or
                s.identity(path) != identity or g.read(path + 0x24C, 1) != before or
                g.read(self.component + 0xD8, 16) != shared or
                g.u64(self.component + 0x100) != current_tree or
                g.read(storage + 0x10, 16) != descriptor or
                (frames and g.read(pointer, len(frames)) != frames)):
            return False
        return before == b'\0' and not active and bool(count)

    def update(self, interact=False):
        # Shift keeps native interaction branches available; only empty-ground
        # click-to-walk remains suppressed. No callback or executable writes.
        try:
            self.validate_local()
            self.validate_remotes()
            self.periodic_audit()
            if not self.asset_valid():
                raise RuntimeError('Click input asset retired or rebuilt')
            current = self.game.read(self.address, 1)[0]
            if current & MASK:
                self.ready = False
                if not self.path_idle():
                    return False
                if not self.asset_valid():
                    raise RuntimeError('Click input asset changed before suppression')
                current = self.game.read(self.address, 1)[0]
                self.applied = True  # Preserve rollback even if the write fails.
                self.game.write(self.address, bytes([current & ~MASK]))
                if self.game.read(self.address, 1)[0] & MASK:
                    raise RuntimeError('Click path suppression readback failed')
                # Selection may already have been in flight. Keep the bit
                # disabled, and wait for a second coherent idle observation on
                # a later update before declaring the mouse aliases ready.
                return False
            if not self.applied:
                raise RuntimeError('Native click enabled bit changed before acquisition')
            if not self.ready:
                if not self.path_idle():
                    return False
                if not self.asset_valid():
                    raise RuntimeError('Click input asset changed before readiness')
                self.ready = True
            return True
        except (OSError, RuntimeError, ValueError, KeyError):
            self.restore()
            if self.restore_failures:
                raise RuntimeError('Click path guard failed and restoration is incomplete') from None
            raise

    def restore(self):
        self.restore_failures.clear()
        if not self.applied:
            return
        try:
            if self.asset_valid():
                if not self.asset_valid():
                    self.applied = self.ready = False
                    return
                # Read the shared byte after the longer ownership checks, so
                # native changes to its unrelated bits are not overwritten.
                current = self.game.read(self.address, 1)[0]
                desired = (current & ~MASK) | self.original
                if self.game.read(self.tree + 0x50, 16) != self.descriptor:
                    raise RuntimeError('Live click state allocation changed before restore')
                if current != desired:
                    self.game.write(self.address, bytes([desired]))
                if self.game.read(self.address, 1)[0] & MASK != self.original:
                    raise RuntimeError('Click path restoration readback failed')
            self.applied = False
            self.ready = False
            self.next_audit = 0.
            self.audit_scan = None
        except (OSError, RuntimeError, ValueError, KeyError):
            self.restore_failures.add('native click path enabled bit')
