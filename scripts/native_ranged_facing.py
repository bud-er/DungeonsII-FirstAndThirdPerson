"""Read the local bow's native rotate-before-fire state without modifying it.

The camera can keep its independent view while native ControlRotation is
temporarily allowed to finish this state. No StateTree, target, input, ability,
owning pointer, transform or executable byte is written here.
"""
from native_layout import matches as native_matches, rva as native_rva

import struct


class RangedStateRebuilt(RuntimeError):
    """The native input tree changed during a bounded read-only snapshot."""


NATIVE_GUARDS = {
    # PlayerStateTreeComponent destroys its shared StateTreeInstanceData at +D8.
    0x57D22B2: bytes.fromhex('488d8bd8000000e8b2a7d5ff'),
    0x552CA76: bytes.fromhex('488bd9488b09e81f400000488b5b08'),
    # FaceTargetOrLocation waits for native facing, then succeeds; otherwise it
    # sets native controller rotation and returns Running.
    0x63C09EA: bytes.fromhex('f30f1015f618ae02488d542448488bcb'),
    0x63C09FA: bytes.fromhex('e8211efcff84c0740db002'),
    0x63C0A10: bytes.fromhex('488d542448488bcbe8d3fffcff32c0'),
}
TREE_NAMES = ('ST_PlayerInput_Controller', 'ST_PlayerInput')


def array_descriptor(raw, maximum):
    pointer, count, capacity = struct.unpack('<Qii', raw)
    if not 0 <= count <= capacity <= maximum or (count and pointer < 0x10000):
        raise RuntimeError('Invalid local ranged state array')
    return pointer, count


def active_state_indices(raw):
    if len(raw) != 0x68 or raw[0x3C] > 8:
        raise RuntimeError('Invalid local ranged active state count')
    return struct.unpack_from('<8H', raw, 0x2C)[:raw[0x3C]]


class NativeRangedFacing:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.trees = {}
        self.component, self.component_identity = None, None
        for name, size in (('PlayerStateTreeComponent', 0x130), ('StateTree', 0x240),
                           ('StateTreeInstanceStorage', 0xE0), ('StateTreeExecutionState', 0x78),
                           ('StateTreeExecutionFrame', 0x68), ('StateTreeActiveStates', 0x34),
                           ('CompactStateTreeState', 0x60)):
            session.ensure_layout(name)
            if session.layouts[name]['size'] != size:
                raise RuntimeError('Unsupported native ranged state layout')
        checks = (('StateTreeInstanceStorage','ExecutionState',0x10,0x78),
                  ('StateTreeExecutionState','ActiveFrames',0,16),
                  ('StateTreeExecutionFrame','StateTree',0,8),
                  ('StateTreeExecutionFrame','ActiveStates',0xC,0x34),
                  ('StateTreeActiveStates','States',0x20,2),
                  ('StateTreeActiveStates','NumStates',0x30,1),
                  ('StateTree','Nodes',0x70,16), ('StateTree','States',0x50,16),
                  ('PlayerStateTreeComponent','StateTreeRef',0x100,40),
                  ('CompactStateTreeState','Name',0x10,8),
                  ('CompactStateTreeState','Parent',0x2A,2),
                  ('CompactStateTreeState','TasksBegin',0x36,2),
                  ('CompactStateTreeState','TasksNum',0x56,1))
        for cls, name, offset, size in checks:
            prop = next((p for p in session.layouts[cls]['properties'] if p['name']==name), {})
            if (prop.get('offset'),prop.get('size')) != (offset,size):
                raise RuntimeError('Unsupported native ranged state field')
        for rva, expected in NATIVE_GUARDS.items():
            if not native_matches(self.game, rva, expected):
                raise RuntimeError('Native ranged facing instruction check failed')
        self.bind_component()

    def fresh_header(self, obj):
        self.session.reflection.headers.pop(obj, None)
        return self.session.reflection.header(obj)

    def bind_component(self):
        s = self.session
        obj = s.pointer(s.pc,'GameplayPlayerController','StateTreeComponent')
        if not obj:
            raise RangedStateRebuilt('Local input state component is changing')
        h = self.fresh_header(obj)
        if h['outer'] != s.pc or not s.is_a(h['class'],'PlayerStateTreeComponent'):
            raise RuntimeError('Ranged state component is not owned by the local controller')
        identity = s.identity(obj)
        if s.pointer(s.pc,'GameplayPlayerController','StateTreeComponent') != obj:
            raise RangedStateRebuilt('Local input state component changed')
        self.component, self.component_identity = obj, identity
        self.trees.clear()

    def component_valid(self):
        s = self.session
        if s.identity(s.pc) != s.identities[s.pc] or s.pointer(s.pc,'Controller','Pawn') != s.pawn:
            raise RuntimeError('Local controller changed during ranged handoff')
        obj = s.pointer(s.pc,'GameplayPlayerController','StateTreeComponent')
        retired = getattr(s,'object_retired',None)
        if obj != self.component or (retired and retired(obj,self.component_identity) is True):
            self.bind_component()
        elif s.identity(obj) != self.component_identity:
            self.bind_component()

    def read_tree(self, tree):
        s, g = self.session, self.game
        h = self.fresh_header(tree)
        if h['name'] not in TREE_NAMES or not s.is_a(h['class'],'StateTree') or h['flags']&0x30:
            raise RuntimeError('Unexpected tree in local ranged execution')
        identity = s.identity(tree)
        states_desc, nodes_desc = g.read(tree+0x50,16), g.read(tree+0x70,16)
        states_ptr, count = array_descriptor(states_desc,128)
        nodes_ptr, size, node_count = struct.unpack('<Qii',nodes_desc)
        if not 0 < node_count <= 256 or not node_count*16 <= size <= 65536 or nodes_ptr < 0x10000:
            raise RuntimeError('Invalid local ranged node container')
        states = g.read(states_ptr,count*0x60) if count else b''
        nodes = g.read(nodes_ptr,size)
        def task_types(row):
            begin, number = struct.unpack_from('<H',row,0x36)[0], row[0x56]
            if begin+number > node_count:
                raise RuntimeError('Invalid local ranged task range')
            types=[]
            for i in range(begin,begin+number):
                typ, offset = struct.unpack_from('<QI',nodes,size-(i+1)*16)
                if offset+0x28 > size-node_count*16:
                    raise RuntimeError('Invalid local ranged node extent')
                types.append(typ)
            return types
        facing = s.native['STT_FaceTargetOrLocation']
        attack = s.native['STT_PlayerRangedAttack']
        indices=[]
        for i in range(count):
            row=states[i*0x60:(i+1)*0x60]
            if facing not in task_types(row):
                continue
            if s.reflection.name(*struct.unpack_from('<II',row,0x10)) != 'RangedRotate' or i+1>=count:
                raise RuntimeError('Unexpected ranged facing state')
            following=states[(i+1)*0x60:(i+2)*0x60]
            if row[0x2A:0x2C] != following[0x2A:0x2C] or attack not in task_types(following):
                raise RuntimeError('Ranged facing does not precede native ranged attack')
            indices.append(i)
        if len(indices)!=1:
            raise RuntimeError('Expected exactly one native ranged facing state')
        if g.read(tree+0x50,16)!=states_desc or g.read(tree+0x70,16)!=nodes_desc or s.identity(tree)!=identity:
            raise RangedStateRebuilt('Local ranged tree rebuilt during discovery')
        result=(identity,states_desc,nodes_desc,count,frozenset(indices))
        self.trees[tree]=result
        return result

    def snapshot(self):
        self.read_context = {}
        try:
            return self.read_snapshot()
        except RangedStateRebuilt:
            raise
        except (OSError,RuntimeError):
            # A native storage replacement can free the previous allocation
            # between reads. Only a changed, still-readable owner chain proves
            # this is a transient rebuild; unchanged read failures propagate.
            if self.changed_after_read_failure():
                raise RangedStateRebuilt('Native ranged storage replaced during read') from None
            raise

    def changed_after_read_failure(self):
        s,g,context = self.session,self.game,self.read_context
        component=context.get('component')
        if component is None:
            return False
        try:
            if s.pointer(s.pc,'GameplayPlayerController','StateTreeComponent')!=component:
                return True
            retired=getattr(s,'object_retired',None)
            if retired and retired(component,context['identity']) is True:
                return True
            if s.identity(component)!=context['identity']:
                return True
            if 'shared' in context and g.read(component+0xD8,16)!=context['shared']:
                return True
            if 'tree' in context and g.u64(component+0x100)!=context['tree']:
                return True
            if 'descriptor' in context and g.read(context['storage']+0x10,16)!=context['descriptor']:
                return True
            if 'frames' in context and context['frames'] and g.read(context['pointer'],len(context['frames']))!=context['frames']:
                return True
        except (OSError,RuntimeError,ValueError):
            return False
        return False

    def read_snapshot(self):
        s,g = self.session,self.game
        self.component_valid()
        component=self.component
        self.read_context.update(component=component,identity=self.component_identity)
        shared=g.read(component+0xD8,16)
        self.read_context['shared']=shared
        storage,control=struct.unpack('<QQ',shared)
        if storage < 0x10000 or control < 0x10000:
            raise RangedStateRebuilt('Local ranged storage is changing')
        descriptor=g.read(storage+0x10,16)
        self.read_context.update(storage=storage,descriptor=descriptor)
        pointer,count=array_descriptor(descriptor,8)
        frames=g.read(pointer,count*0x68) if count else b''
        self.read_context.update(pointer=pointer,frames=frames)
        active_tree=g.u64(component+0x100)
        self.read_context['tree']=active_tree
        waiting=False
        for i in range(count):
            raw=frames[i*0x68:(i+1)*0x68]
            tree=struct.unpack_from('<Q',raw)[0]
            if tree != active_tree:
                raise RangedStateRebuilt('Local input method is changing')
            info=self.trees.get(tree)
            if info is not None:
                retired=getattr(s,'object_retired',None)
                if retired and retired(tree,info[0]) is True:
                    raise RangedStateRebuilt('Local ranged tree retired')
                if s.identity(tree)!=info[0]:
                    self.trees.pop(tree,None);info=None
                elif g.read(tree+0x50,16)!=info[1] or g.read(tree+0x70,16)!=info[2]:
                    self.trees.pop(tree,None);info=None
            if info is None:
                info=self.read_tree(tree)
            states=active_state_indices(raw)
            if any(index>=info[3] for index in states):
                raise RuntimeError('Invalid local ranged active state index')
            waiting |= bool(info[4].intersection(states))
        if (s.pointer(s.pc,'GameplayPlayerController','StateTreeComponent')!=component or
                s.identity(component)!=self.component_identity or
                g.read(component+0xD8,16)!=shared or g.u64(component+0x100)!=active_tree or
                g.read(storage+0x10,16)!=descriptor or
                (count and g.read(pointer,count*0x68)!=frames)):
            raise RangedStateRebuilt('Local ranged execution changed during snapshot')
        return waiting

    def waiting(self):
        for attempt in range(2):
            try:
                return self.snapshot()
            except RangedStateRebuilt:
                if attempt:
                    raise
