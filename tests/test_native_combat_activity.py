from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from native_combat_activity import ActivityUnavailable, NativeCombatActivity


class Memory:
    def __init__(self):
        self.data = {}
        self.write = Mock(side_effect=AssertionError('Activity reader must never write'))
    def put(self, address, data):
        self.data.update((address+i, value) for i, value in enumerate(data))
    def read(self, address, size):
        return bytes(self.data[address+i] for i in range(size))
    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]


class NativeActivityTests(unittest.TestCase):
    def make(self, extra=('GA_Artifact', 'GA_HandleThrowable', 'GA_PassivePlayerSoulRegeneration')):
        g=Memory();headers={};identities={};parents={};rows=[];entries={}
        def obj(address, cls, flags=0, outer=0):
            raw=struct.pack('<QIIQIIQ',0,flags,address//16,cls,address//32,0,outer)
            g.put(address,raw)
            identities[address]=(raw[0xC:0x20],struct.pack('<I',1))
            headers[address]={'class':cls,'flags':flags,'outer':outer}
        obj(0x3000,99,outer=0x2000);parents[99]={'AbilitySystemComponent'}
        for i,name in enumerate(('GA_PlayerMeleeAttack','GA_PlayerRangedAttack','GA_PlayerRangedAim','GA_Roll')+extra):
            cls=100+i;parents[cls]={name,'GameplayAbility'}
            prototype=0x10000+i*0x2000;instance=prototype+0x1000
            obj(prototype,cls,0x10);g.put(instance,b'\0'*0x389);obj(instance,cls,outer=0x2000)
            instance_array=0x80000+i*0x100
            g.put(instance_array,struct.pack('<Q',instance))
            row=bytearray(0xF0);struct.pack_into('<I',row,0xC,i+1)
            struct.pack_into('<Q',row,0x10,prototype)
            struct.pack_into('<Qii',row,0x78,instance_array,1,1)
            rows.append(row);entries.setdefault(name,[]).append((i,prototype,instance))
        def prop(name,kind,offset,size,target=None):
            p=dict(name=name,type=kind,offset=offset,size=size,dimensions=1)
            if target:p['target']=target
            return p
        active=prop('bIsActive','BoolProperty',0x388,1)
        active['bool']=dict(fieldSize=1,byteOffset=0,byteMask=1,fieldMask=255)
        layouts={
          'BaseCharacter':{'properties':[]},
          'AbilitySystemComponent':{'properties':[prop('ActivatableAbilities','StructProperty',0x400,0x120,'GameplayAbilitySpecContainer')]},
          'GameplayAbilitySpecContainer':{'properties':[prop('Items','ArrayProperty',0x108,16),prop('Owner','ObjectProperty',0x118,8,'AbilitySystemComponent')]},
          'GameplayAbilitySpec':{'size':0xF0,'properties':[prop('Ability','ObjectProperty',0x10,8,'GameplayAbility'),prop('ActiveCount','ByteProperty',0x28,1),prop('NonReplicatedInstances','ArrayProperty',0x78,16),prop('ReplicatedInstances','ArrayProperty',0x88,16)]},
          'GameplayAbility':{'properties':[active]}}
        s=SimpleNamespace(game=g,pawn=0x2000,pc=0x1000,asc=0x3000,layouts=layouts,
                          local_scope=SimpleNamespace(validate=Mock()),ensure_layout=lambda _:None)
        s.pointer=lambda obj,cls,name:s.asc
        s.identity=lambda obj:identities[obj]
        s.object_retired=lambda obj,expected:identities.get(obj)!=expected
        s.reflection=SimpleNamespace(headers={},header=lambda obj:headers[obj])
        s.is_a=lambda cls,name:name in parents[cls]
        def sync():
            g.put(0x3508,struct.pack('<Qii',0x70000,len(rows),len(rows)))
            g.put(0x3518,struct.pack('<Q',s.asc))
            g.put(0x70000,b''.join(rows))
        sync()
        return NativeCombatActivity(s),g,s,rows,entries,identities,headers,sync

    def test_idle_required_coverage_and_passives_do_not_block_movement(self):
        a,g,_,rows,e,_,_,sync=self.make()
        i,_,instance=e['GA_PassivePlayerSoulRegeneration'][0]
        rows[i][0x28]=1;g.put(instance+0x388,b'\1');sync()
        self.assertEqual(a.snapshot(),(False,False));g.write.assert_not_called()

    def test_all_known_attack_and_charge_instances_keep_combat_priority(self):
        for name in ('GA_PlayerMeleeAttack','GA_PlayerRangedAttack','GA_PlayerRangedAim','GA_Artifact','GA_HandleThrowable','GA_HeavyJumpAttack'):
            with self.subTest(name=name):
                a,g,_,_,e,_,_,_=self.make(extra=('GA_Artifact','GA_HandleThrowable','GA_HeavyJumpAttack'))
                g.put(e[name][0][2]+0x388,b'\1')
                self.assertEqual(a.snapshot(),(True,False));g.write.assert_not_called()

    def test_active_count_covers_early_activation_before_instance_creation(self):
        for name,want in (('GA_PlayerRangedAttack',(True,False)),('GA_Roll',(False,True))):
            a,_,_,rows,e,_,_,sync=self.make()
            i,_,_=e[name][0];rows[i][0x28]=1;rows[i][0x78:0x98]=b'\0'*32;sync()
            self.assertEqual(a.snapshot(),want)

    def test_multiple_roll_and_artifact_specs_are_all_observed(self):
        a,g,_,_,e,_,_,_=self.make(extra=('GA_Roll','GA_Artifact','GA_Artifact'))
        g.put(e['GA_Roll'][1][2]+0x388,b'\1')
        g.put(e['GA_Artifact'][1][2]+0x388,b'\1')
        self.assertEqual(a.snapshot(),(True,True))

    def test_missing_required_coverage_is_unavailable_not_idle(self):
        a,_,_,rows,e,_,_,sync=self.make();rows.pop(e['GA_Roll'][0][0]);sync()
        with self.assertRaisesRegex(ActivityUnavailable,'Required local combat'):
            a.snapshot()

    def test_foreign_ability_system_and_remote_instance_are_refused(self):
        for foreign_asc in (True,False):
            a,g,_,_,e,_,headers,_=self.make()
            if foreign_asc:headers[0x3000]['outer']=0x9999
            else:g.put(e['GA_Roll'][0][2]+0x20,struct.pack('<Q',0x9999))
            with self.assertRaisesRegex(ActivityUnavailable,'owned by the local pawn'):
                a.snapshot()
            g.write.assert_not_called()

    def test_retired_tracked_instance_is_not_dereferenced_and_next_read_recovers(self):
        a,g,_,_,e,ids,_,_=self.make();self.assertEqual(a.snapshot(),(False,False))
        instance=e['GA_Roll'][0][2];identity=ids.pop(instance)
        read=g.read
        def reject_old(address,size):
            if address==instance:raise AssertionError('Retired instance dereferenced')
            return read(address,size)
        g.read=reject_old
        with self.assertRaisesRegex(ActivityUnavailable,'retired'):a.snapshot()
        ids[instance]=identity;g.read=read
        self.assertEqual(a.snapshot(),(False,False))

    def test_unreadable_live_optional_data_is_unavailable_without_scope_failure(self):
        a,g,s,_,_,_,_,_=self.make();read=g.read
        g.read=lambda address,size: (_ for _ in ()).throw(OSError(299,'live array unreadable')) if address==0x3508 else read(address,size)
        with self.assertRaisesRegex(ActivityUnavailable,'live array unreadable'):a.snapshot()
        s.local_scope.validate.assert_called_once();g.write.assert_not_called()

    def test_possession_changes_are_fatal_and_never_wrapped_as_optional(self):
        a,_,s,_,_,_,_,_=self.make();s.local_scope.validate.side_effect=RuntimeError('possession changed')
        with self.assertRaises(RuntimeError) as caught:a.snapshot()
        self.assertNotIsInstance(caught.exception,ActivityUnavailable)

    def test_count_instance_handoff_is_not_published_as_idle(self):
        a,g,_,_,_,_,_,_=self.make();read=g.read;reads=[0]
        def changing(address,size):
            raw=read(address,size)
            if address==0x70000:
                reads[0]+=1
                if reads[0]==2:
                    value=bytearray(raw);value[0x28]=1;return bytes(value)
            return raw
        g.read=changing
        with self.assertRaisesRegex(ActivityUnavailable,'changed during capture'):a.snapshot()
        self.assertTrue(a.prototypes)  # No expensive cold reset for ordinary handoffs.

    def test_unrelated_spec_prediction_bookkeeping_does_not_invalidate(self):
        a,g,_,_,_,_,_,_=self.make();read=g.read;reads=[0]
        def changing(address,size):
            raw=read(address,size)
            if address==0x70000:
                reads[0]+=1
                if reads[0]==2:
                    value=bytearray(raw);value[0x29]^=1;return bytes(value)
            return raw
        g.read=changing;self.assertEqual(a.snapshot(),(False,False))

    def test_new_spec_handle_does_not_reuse_cached_passive_classification(self):
        a,g,_,rows,e,ids,headers,sync=self.make();a.snapshot()
        i,prototype,instance=e['GA_PassivePlayerSoulRegeneration'][0]
        struct.pack_into('<I',rows[i],0xC,99)
        # Replacing a passive prototype with a combat prototype must be reclassified.
        rows[i][0x10:0x18]=struct.pack('<Q',e['GA_PlayerMeleeAttack'][0][1])
        rows[i][0x28]=1;rows[i][0x78:0x98]=b'\0'*32;sync()
        self.assertEqual(a.snapshot(),(True,False))

    def test_layout_mismatch_is_unavailable(self):
        a,_,s,_,_,_,_,_=self.make()
        s.layouts['GameplayAbilitySpec']['size']=0x100
        with self.assertRaises(ActivityUnavailable):NativeCombatActivity(s)


if __name__=='__main__':unittest.main()
