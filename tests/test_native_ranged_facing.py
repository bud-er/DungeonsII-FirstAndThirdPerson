from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from native_ranged_facing import NativeRangedFacing, RangedStateRebuilt, NATIVE_GUARDS


class ReadOnlyMemory:
    def __init__(self):
        self.base=0x10000000
        self.data={}
        self.on_read=None
    def put(self,address,data):
        self.data.update((address+i,b) for i,b in enumerate(data))
    def read(self,address,size):
        result=bytes(self.data.get(address+i,0) for i in range(size))
        if self.on_read:self.on_read(address,size)
        return result
    def u64(self,address):return struct.unpack('<Q',self.read(address,8))[0]
    def write(self,*args):raise AssertionError('Ranged facing reader must never write')


def fixture(tree_name='ST_PlayerInput_Controller'):
    g=ReadOnlyMemory()
    pc,pawn,component,tree=0x10000,0x20000,0x30000,0x40000
    storage,frames,states,nodes=0x50000,0x60000,0x70000,0x80000
    identities={pc:1,pawn:2,component:3,tree:4}
    headers={component:{'outer':pc,'class':'PlayerStateTreeComponent','flags':0},
             tree:{'outer':0x90000,'class':'StateTree','flags':0,'name':tree_name}}
    s=SimpleNamespace(game=g,pc=pc,pawn=pawn,identities=identities,component=component,
                      native={'STT_FaceTargetOrLocation':0xA0000,'STT_PlayerRangedAttack':0xA1000})
    s.identity=lambda obj:identities[obj]
    s.is_a=lambda cls,name:cls==name
    s.object_retired=lambda *args:False
    s.pointer=lambda obj,cls,name:s.component if name=='StateTreeComponent' else pawn
    s.reflection=SimpleNamespace(headers={},header=lambda obj:headers[obj],
                                 name=lambda index,number:{1:'Idle',2:'RangedRotate',3:'RangedFire'}[index])
    specs={
        'PlayerStateTreeComponent':(0x130,[('StateTreeRef',0x100,40)]),
        'StateTree':(0x240,[('States',0x50,16),('Nodes',0x70,16)]),
        'StateTreeInstanceStorage':(0xE0,[('ExecutionState',0x10,0x78)]),
        'StateTreeExecutionState':(0x78,[('ActiveFrames',0,16)]),
        'StateTreeExecutionFrame':(0x68,[('StateTree',0,8),('ActiveStates',0xC,0x34)]),
        'StateTreeActiveStates':(0x34,[('States',0x20,2),('NumStates',0x30,1)]),
        'CompactStateTreeState':(0x60,[('Name',0x10,8),('Parent',0x2A,2),
                                      ('TasksBegin',0x36,2),('TasksNum',0x56,1)])}
    s.layouts={n:{'size':size,'properties':[dict(name=p,offset=o,size=l) for p,o,l in fields]}
               for n,(size,fields) in specs.items()}
    s.ensure_layout=lambda name:None
    for rva,expected in NATIVE_GUARDS.items():g.put(g.base+rva,expected)
    g.put(component+0xD8,struct.pack('<QQ',storage,storage-16))
    g.put(component+0x100,struct.pack('<Q',tree))
    g.put(storage+0x10,struct.pack('<Qii',frames,1,4))
    frame=bytearray(0x68);struct.pack_into('<Q',frame,0,tree)
    frame[0x3C]=1;g.put(frames,frame)
    rows=bytearray(3*0x60)
    for i in range(3):
        struct.pack_into('<II',rows,i*0x60+0x10,i+1,0)
        struct.pack_into('<H',rows,i*0x60+0x2A,0)
    struct.pack_into('<H',rows,0x60+0x36,0);rows[0x60+0x56]=1
    struct.pack_into('<H',rows,0xC0+0x36,1);rows[0xC0+0x56]=1
    g.put(tree+0x50,struct.pack('<Qii',states,3,3));g.put(states,rows)
    body=bytearray(0x80)
    struct.pack_into('<QII',body,0x70,s.native['STT_FaceTargetOrLocation'],0,0)
    struct.pack_into('<QII',body,0x60,s.native['STT_PlayerRangedAttack'],0x30,0)
    g.put(tree+0x70,struct.pack('<Qii',nodes,len(body),2));g.put(nodes,body)
    return SimpleNamespace(s=s,g=g,headers=headers,ids=identities,component=component,
                           tree=tree,storage=storage,frames=frames,states=states,nodes=nodes)


class NativeRangedFacingTests(unittest.TestCase):
    def test_both_input_trees_wait_only_in_verified_rotate_before_fire_state(self):
        for name in ('ST_PlayerInput_Controller','ST_PlayerInput'):
            with self.subTest(name=name):
                f=fixture(name);p=NativeRangedFacing(f.s)
                self.assertFalse(p.waiting())
                f.g.put(f.frames+0x2C,struct.pack('<H',1));self.assertTrue(p.waiting())
                f.g.put(f.frames+0x2C,struct.pack('<H',2));self.assertFalse(p.waiting())

    def test_facing_task_outside_ranged_sequence_is_refused(self):
        f=fixture();f.g.put(f.states+0x60+0x10,struct.pack('<II',1,0))
        with self.assertRaisesRegex(RuntimeError,'Unexpected ranged facing'):NativeRangedFacing(f.s).waiting()
        f=fixture();f.g.put(f.nodes+0x60,struct.pack('<Q',0xBAD00))
        with self.assertRaisesRegex(RuntimeError,'precede'):NativeRangedFacing(f.s).waiting()

    def test_native_guard_and_reflected_layout_changes_are_rejected(self):
        f=fixture();rva=next(iter(NATIVE_GUARDS));f.g.put(f.g.base+rva,b'\x90')
        with self.assertRaisesRegex(RuntimeError,'instruction'):NativeRangedFacing(f.s)
        f=fixture();f.s.layouts['StateTreeExecutionFrame']['size']+=8
        with self.assertRaisesRegex(RuntimeError,'layout'):NativeRangedFacing(f.s)

    def test_foreign_component_or_unrecognized_tree_is_rejected(self):
        f=fixture();f.headers[f.component]['outer']=f.s.pawn
        with self.assertRaisesRegex(RuntimeError,'owned'):NativeRangedFacing(f.s)
        f=fixture();p=NativeRangedFacing(f.s);f.headers[f.tree]['name']='ST_RemotePlayer'
        with self.assertRaisesRegex(RuntimeError,'Unexpected tree'):p.waiting()

    def test_input_method_switch_rebinds_current_tree_without_old_frame_guess(self):
        f=fixture();p=NativeRangedFacing(f.s);self.assertFalse(p.waiting())
        f.g.put(f.component+0x100,struct.pack('<Q',f.tree+8))
        with self.assertRaises(RangedStateRebuilt):p.waiting()

    def test_native_state_change_during_read_retries_coherently(self):
        f=fixture();p=NativeRangedFacing(f.s);self.assertFalse(p.waiting())
        def change(address,size):
            if address==f.frames:
                f.g.on_read=None
                f.g.put(f.frames+0x2C,struct.pack('<H',1))
        f.g.on_read=change
        self.assertTrue(p.waiting())

    def test_persistent_rebuild_is_bounded_and_reported(self):
        f=fixture();p=NativeRangedFacing(f.s);reads=[]
        def change(address,size):
            if address==f.frames:
                reads.append(address)
                f.g.put(f.frames+0x2C,struct.pack('<H',len(reads)%2))
        f.g.on_read=change
        with self.assertRaises(RangedStateRebuilt):p.waiting()
        self.assertLessEqual(len(reads),4)

    def test_component_replaced_with_foreign_owner_is_not_used(self):
        f=fixture();p=NativeRangedFacing(f.s)
        f.s.component=0xB0000;f.ids[f.s.component]=9
        f.headers[f.s.component]={'outer':f.s.pawn,'class':'PlayerStateTreeComponent'}
        with self.assertRaisesRegex(RuntimeError,'owned'):p.waiting()

    def test_proven_tree_retirement_is_not_dereferenced(self):
        f=fixture();p=NativeRangedFacing(f.s);p.waiting()
        f.s.object_retired=lambda obj,identity:obj==f.tree
        f.s.identity=lambda obj:(_ for _ in ()).throw(AssertionError('retired tree dereference')) if obj==f.tree else f.ids[obj]
        with self.assertRaises(RangedStateRebuilt):p.waiting()

    def test_malformed_counts_and_state_indices_are_rejected(self):
        for address,value in (('count',b'\x09'),('index',struct.pack('<H',100))):
            f=fixture();p=NativeRangedFacing(f.s)
            f.g.put(f.frames+(0x3C if address=='count' else 0x2C),value)
            with self.assertRaisesRegex(RuntimeError,'active state'):p.waiting()

    def test_freed_old_storage_is_retried_only_after_owner_changes(self):
        f=fixture();p=NativeRangedFacing(f.s);original=f.g.read
        replacement=f.storage+0x1000
        f.g.put(replacement+0x10,original(f.storage+0x10,16))
        def read(address,size):
            if address==f.storage+0x10:
                f.g.put(f.component+0xD8,struct.pack('<QQ',replacement,replacement-16))
                raise OSError('old storage freed')
            return original(address,size)
        f.g.read=read
        self.assertFalse(p.waiting())

    def test_unchanged_unreadable_storage_stays_a_real_error(self):
        f=fixture();p=NativeRangedFacing(f.s);original=f.g.read
        def read(address,size):
            if address==f.storage+0x10:raise OSError('unchanged storage unreadable')
            return original(address,size)
        f.g.read=read
        with self.assertRaisesRegex(OSError,'unchanged storage'):p.waiting()

    def test_torn_frame_bad_count_retries_only_when_frame_changed(self):
        f=fixture();p=NativeRangedFacing(f.s)
        f.g.put(f.frames+0x3C,b'\x09')
        def change(address,size):
            if address==f.frames:
                f.g.on_read=None
                f.g.put(f.frames+0x3C,b'\x01')
                f.g.put(f.frames+0x2C,struct.pack('<H',1))
        f.g.on_read=change
        self.assertTrue(p.waiting())

    def test_freed_frame_buffer_retries_after_descriptor_changes(self):
        f=fixture();p=NativeRangedFacing(f.s);original=f.g.read
        replacement=f.frames+0x1000
        f.g.put(replacement,original(f.frames,0x68))
        def read(address,size):
            if address==f.frames:
                f.g.put(f.storage+0x10,struct.pack('<Qii',replacement,1,4))
                raise OSError('old frames freed')
            return original(address,size)
        f.g.read=read
        self.assertFalse(p.waiting())


if __name__=='__main__':unittest.main()
