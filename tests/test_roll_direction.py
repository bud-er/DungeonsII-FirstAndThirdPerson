import contextlib
import io
import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from roll_direction import (RollDirection, RollUnavailable, RollStateRebuilt,
                            CONSTANT_FORCE_VTABLE, stick_yaw, KEYBOARD_DODGE)


class Memory:
    base=0x100000
    def __init__(self):self.bytes={};self.writes=[]
    def load(self,address,data):self.bytes.update((address+i,v) for i,v in enumerate(data))
    def read(self,address,size):return bytes(self.bytes.get(address+i,0) for i in range(size))
    def u64(self,address):return struct.unpack('<Q',self.read(address,8))[0]
    def write(self,address,data):self.writes.append((address,data));self.load(address,data)


class RollDirectionTests(unittest.TestCase):
    def profile(self):
        f=RollDirection.__new__(RollDirection)
        f.game=Memory();f.movement=15000;f.curve=200000
        f.curve_identity=2
        f.session=SimpleNamespace(pawn=5000,yaw=20.,identities={15000:1,200000:2})
        f.session.identity=Mock(side_effect=lambda obj:f.session.identities[obj])
        f.session.pointer=Mock(return_value=15000)
        f.headers=(16000,16016);f.seen=set();f.last_buttons=0;f.request=None
        for header in f.headers:f.game.load(header,struct.pack('<Qii',0,0,0))
        return f

    def source(self,f,local_id=23,age=.01,curve=None,local=False,status=0,pending=False):
        raw=bytearray(224)
        struct.pack_into('<Q',raw,0,f.game.base+CONSTANT_FORCE_VTABLE)
        struct.pack_into('<H',raw,0xA,local_id)
        struct.pack_into('<f',raw,0x18,age)
        struct.pack_into('<f',raw,0x20,.5)
        raw[0x24]=status;raw[0x27]=int(local)
        struct.pack_into('<ddd',raw,0xC0,750.,0.,7.)
        struct.pack_into('<Q',raw,0xD8,f.curve if curve is None else curve)
        f.game.load(100000,raw)
        f.game.load(25000,struct.pack('<QQ',100000,120000))
        f.game.load(f.headers[int(pending)],struct.pack('<Qii',25000,1,4))

    def update(self,f,now):
        with contextlib.redirect_stdout(io.StringIO()):f.update(now=now)

    def test_stick_directions_are_relative_to_the_view(self):
        self.assertEqual(stick_yaw(20,0,-1),-160.)
        self.assertEqual(stick_yaw(20,1,0),110.)
        self.assertEqual(stick_yaw(20,-1,0),-70.)
        self.assertEqual(stick_yaw(20,0,0),20.)
        with self.assertRaises(RuntimeError):stick_yaw(20,math.nan,0)

    def test_new_pending_roll_changes_only_direction_preserving_strength_and_z(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f,pending=True);self.update(f,10.01)
        self.assertEqual(len(f.game.writes),1)
        self.assertEqual(f.game.writes[0][0],100000+0xC0)
        x,y,z=struct.unpack('<ddd',f.game.writes[0][1])
        self.assertAlmostEqual(math.hypot(x,y),750.)
        self.assertAlmostEqual(math.degrees(math.atan2(y,x)),-160.)
        self.assertEqual(z,7.)
        self.assertEqual(f.session.yaw,20.)
        self.assertIsNone(f.request)

    def test_keyboard_r_steers_a_new_native_roll_in_each_wasd_direction(self):
        for axes, expected in (((0,1),20),((1,0),110),((-1,0),-70),((0,-1),-160)):
            f=self.profile();f.set_input(KEYBOARD_DODGE,axes,now=10.)
            self.source(f,pending=True);self.update(f,10.01)
            with self.subTest(axes=axes):
                self.assertEqual(len(f.game.writes),1)
                x,y,z=struct.unpack('<ddd',f.game.writes[0][1])
                self.assertAlmostEqual(math.degrees(math.atan2(y,x)),expected)
                self.assertAlmostEqual(math.hypot(x,y),750.)
                self.assertEqual(z,7.)

    def test_keyboard_hold_focus_return_and_cooldown_do_not_redirect_existing_rolls(self):
        f=self.profile();f.set_input(KEYBOARD_DODGE,(0,-1),eligible=False,now=10.)
        f.set_input(KEYBOARD_DODGE,(0,-1),now=10.01)
        self.source(f);self.update(f,10.02)
        f.set_input(0,(0,-1),now=10.03)
        f.set_input(KEYBOARD_DODGE,(1,0),now=10.04);self.update(f,10.05)
        self.assertFalse(f.game.writes)

    def test_new_active_roll_can_be_steered_once(self):
        f=self.profile();f.set_input(0x80,(1,0),now=10.)
        self.source(f);self.update(f,10.01);self.update(f,10.02)
        self.assertEqual(len(f.game.writes),1)
        x,y,_=struct.unpack('<ddd',f.game.writes[0][1])
        self.assertAlmostEqual(math.degrees(math.atan2(y,x)),110.)

    def test_cooldown_press_cannot_redirect_an_existing_roll(self):
        f=self.profile();self.source(f);self.update(f,10.)
        f.set_input(0x80,(0,-1),now=10.01);self.update(f,10.02)
        self.assertEqual(f.game.writes,[])

    def test_held_r3_cannot_steer_another_roll_or_rearm_request(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f);self.update(f,10.01)
        f.set_input(0x80,(1,0),now=10.02)
        self.source(f,local_id=24);self.update(f,10.03)
        self.assertEqual(len(f.game.writes),1)

    def test_release_and_new_click_can_steer_a_new_source(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f);self.update(f,10.01)
        f.set_input(0,(0,-1),now=11.)
        f.set_input(0x80,(1,0),now=11.01)
        self.source(f,local_id=24);self.update(f,11.02)
        self.assertEqual(len(f.game.writes),2)

    def test_unfocused_press_and_focus_return_do_not_create_a_request(self):
        f=self.profile();f.set_input(0x80,(0,-1),eligible=False,now=10.)
        f.set_input(0x80,(0,-1),now=10.01);self.source(f);self.update(f,10.02)
        self.assertEqual(f.game.writes,[])

    def test_old_roll_or_expired_request_is_not_steered(self):
        for age,now in ((.2,10.01),(.01,10.2)):
            f=self.profile();f.set_input(0x80,(0,-1),now=10.)
            self.source(f,age=age);self.update(f,now)
            self.assertEqual(f.game.writes,[])

    def test_unrelated_force_local_space_and_finished_sources_are_untouched(self):
        for options in ({'curve':300000},{'local':True},{'status':2},{'status':4}):
            f=self.profile();f.set_input(0x80,(0,-1),now=10.)
            self.source(f,**options);self.update(f,10.01)
            self.assertEqual(f.game.writes,[])

    def test_different_source_type_is_untouched(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f);f.game.load(100000,struct.pack('<Q',f.game.base+123))
        self.update(f,10.01);self.assertEqual(f.game.writes,[])

    def test_array_reallocation_row_replacement_and_source_recycling_abort_write(self):
        for change in ('array','row','source','force'):
            f=self.profile();f.set_input(0x80,(0,-1),now=10.)
            self.source(f);rows=f.sources();f.sources=Mock(return_value=rows)
            if change=='array':f.game.load(16000,struct.pack('<Qii',26000,1,4))
            if change=='row':f.game.load(25000,struct.pack('<QQ',110000,130000))
            if change=='source':f.game.load(100000+0xA,struct.pack('<H',24))
            if change=='force':f.game.load(100000+0xC0,struct.pack('<ddd',1,2,3))
            self.update(f,10.01);self.assertEqual(f.game.writes,[])

    def test_replaced_movement_and_curve_block_writes(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f);f.session.identity.side_effect=None;f.session.identity.return_value=99
        with self.assertRaises(RuntimeError):self.update(f,10.01)
        self.assertEqual(f.game.writes,[])

    def test_retired_curve_is_optional_but_movement_ownership_stays_fatal(self):
        from roll_direction import RollUnavailable
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f)
        f.session.identity.side_effect=lambda obj:99 if obj==f.curve else f.session.identities[obj]
        with self.assertRaises(RollUnavailable):f.update(now=10.01)
        self.assertFalse(f.game.writes)
        f.session.pointer.return_value=987
        with self.assertRaisesRegex(RuntimeError,'movement owner changed'):f.update(now=10.01)

    def test_clear_cancels_pending_input_without_reversing_committed_roll(self):
        f=self.profile();f.set_input(0x80,(0,-1),now=10.)
        self.source(f);self.update(f,10.01)
        f.game.writes.clear();f.clear()
        self.assertEqual(f.game.writes,[])
        self.assertIsNone(f.request);self.assertFalse(f.seen)

    def test_source_array_reallocation_is_retried_from_current_shared_rows(self):
        f=self.profile();self.source(f)
        read=f.game.read;changed=[]
        replacement=struct.pack('<Qii',26000,1,4)
        f.game.load(26000,read(25000,16))
        def replaced(address,size):
            result=read(address,size)
            if address==25000 and not changed:
                changed.append(True);f.game.load(f.headers[0],replacement)
            return result
        f.game.read=replaced
        rows=f.sources()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['descriptor'],replacement)
        self.assertEqual(rows[0]['row'],26000)
        self.assertFalse(f.game.writes)

    def test_freed_array_buffer_can_retire_to_an_empty_coherent_snapshot(self):
        f=self.profile();self.source(f)
        read=f.game.read;attempts=[]
        def freed(address,size):
            if address==25000:
                attempts.append(address)
                f.game.load(f.headers[0],struct.pack('<Qii',0,0,0))
                raise OSError('old shared rows freed')
            return read(address,size)
        f.game.read=freed
        self.assertEqual(f.sources(),[])
        self.assertEqual(len(attempts),1)
        self.assertFalse(f.game.writes)

    def test_persistently_unreadable_source_buffer_suspends_only_optional_steering(self):
        f=self.profile();self.source(f);f.set_input(0x80,(0,-1),now=10.)
        read=f.game.read;attempts=[]
        def unreadable(address,size):
            if address==25000:
                attempts.append(address);raise OSError('source buffer unavailable')
            return read(address,size)
        f.game.read=unreadable
        with self.assertRaisesRegex(RollUnavailable,'source reads remain unavailable'):
            self.update(f,10.01)
        self.assertEqual(len(attempts),2)
        self.assertFalse(f.game.writes)

    def test_continuous_rebuild_defers_with_seen_set_and_request_expiry_preserved(self):
        f=self.profile();self.source(f);f.seen={(100000,120000,22)}
        f.set_input(0x80,(0,-1),now=10.)
        read=f.game.read;attempts=[]
        def rebuild(address,size):
            result=read(address,size)
            if address==25000:
                attempts.append(address)
                f.game.load(f.headers[0],struct.pack('<Qii',25000,1,4+len(attempts)))
            return result
        f.game.read=rebuild
        self.assertIs(f.update(now=10.01),False)
        self.assertEqual(len(attempts),2)
        self.assertEqual(f.seen,{(100000,120000,22)})
        self.assertIsNotNone(f.request)
        self.assertIs(f.update(now=10.2),False)
        self.assertEqual(len(attempts),4)
        self.assertIsNone(f.request)
        self.assertEqual(f.seen,{(100000,120000,22)})
        self.assertFalse(f.game.writes)

    def test_finished_source_before_write_skips_retired_row_without_dereferencing_again(self):
        f=self.profile();self.source(f);f.set_input(0x80,(0,-1),now=10.)
        f.sources=Mock(return_value=f.sources())
        read=f.game.read;attempts=[]
        def finished(address,size):
            if address==100000 and size==224:
                attempts.append(address)
                f.game.load(f.headers[0],struct.pack('<Qii',0,0,0))
                raise OSError('source finished')
            return read(address,size)
        f.game.read=finished
        self.update(f,10.01)
        self.assertEqual(len(attempts),1)
        self.assertFalse(f.game.writes)

    def test_unreadable_candidate_before_write_is_optional_and_never_written(self):
        f=self.profile();self.source(f);f.set_input(0x80,(0,-1),now=10.)
        f.sources=Mock(return_value=f.sources())
        read=f.game.read;attempts=[]
        def unreadable(address,size):
            if address==100000 and size==224:
                attempts.append(address);raise OSError('candidate source unavailable')
            return read(address,size)
        f.game.read=unreadable
        with self.assertRaisesRegex(RollUnavailable,'before steering'):
            self.update(f,10.01)
        self.assertEqual(len(attempts),2)
        self.assertFalse(f.game.writes)

    def test_movement_owner_replacement_during_optional_read_stays_fatal(self):
        f=self.profile();self.source(f)
        read=f.game.read
        def changed_owner(address,size):
            if address==25000:
                f.session.pointer.return_value=987
                raise OSError('source buffer unavailable')
            return read(address,size)
        f.game.read=changed_owner
        with self.assertRaisesRegex(RuntimeError,'movement owner changed') as caught:
            f.sources()
        self.assertNotIsInstance(caught.exception,RollUnavailable)
        self.assertFalse(f.game.writes)

    def test_source_which_aged_past_new_roll_window_is_not_written(self):
        f=self.profile();self.source(f);f.set_input(0x80,(0,-1),now=10.)
        f.sources=Mock(return_value=f.sources())
        f.game.load(100000+0x18,struct.pack('<f',.2))
        self.update(f,10.01)
        self.assertFalse(f.game.writes)

    def test_partial_force_write_clears_request_and_is_not_retried_or_restored(self):
        f=self.profile();self.source(f);f.set_input(0x80,(0,-1),now=10.)
        write=f.game.write
        def partial(address,data):
            write(address,data[:8]);raise OSError('partial native force write')
        f.game.write=partial
        with self.assertRaisesRegex(RuntimeError,'completion is uncertain') as caught:
            self.update(f,10.01)
        self.assertNotIsInstance(caught.exception,RollUnavailable)
        self.assertIn('partial native force write',str(caught.exception))
        self.assertIsNone(f.request)
        self.update(f,10.02);f.clear()
        self.assertEqual(len(f.game.writes),1)
        self.assertEqual(f.game.writes[0][0],100000+0xC0)


if __name__=='__main__':unittest.main()
