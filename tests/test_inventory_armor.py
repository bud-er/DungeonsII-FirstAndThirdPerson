from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from inventory_armor import ButtonEdge, InventoryArmor
from launcher_settings import DEFAULTS, helper_arguments


class InventoryArmorTests(unittest.TestCase):
    def make_control(self):
        a=InventoryArmor.__new__(InventoryArmor)
        a.edge=ButtonEdge();a.hidden_slots=frozenset();a.equipment=Mock()
        a.menu=SimpleNamespace(input_captured=True,names=('MiniInventory',),
            armor_widgets={10:('ten',100),20:('twenty',200)},
            local_scope=Mock(presented=Mock(return_value=True)))
        a.validate=Mock();a.function=300;a.function_identity='function';a.getter=400
        selected={10:True,20:False}
        a.game=Mock(u64=Mock(return_value=400))
        def read(address,size):
            if address==400:return a.SELECTED_GETTER
            if address in (100,200):return struct.pack('<II',address//100,0)
            return bytes([int(selected[address-0x4C1])])
        a.game.read.side_effect=read
        a.session=Mock()
        a.session.identity.side_effect=lambda obj:{10:'ten',20:'twenty',300:'function'}[obj]
        a.session.object_retired.return_value=False
        a.session.reflection.name.side_effect=lambda tag,number:{1:'SW.ItemSlot.Equipment.Armor.Helmet',2:'SW.ItemSlot.Equipment.Armor.Boots'}[tag]
        a.slots=Mock(collect=Mock(return_value={123:'armor'}))
        return a,selected

    def test_selected_local_equipped_slot_only(self):
        a,selected=self.make_control()
        self.assertEqual(a.selected_slot(),'helmet')
        selected.update({10:False,20:True})
        self.assertEqual(a.selected_slot(),'boots')
        selected[10]=True
        self.assertIsNone(a.selected_slot())

    def test_stale_hidden_and_empty_slots_never_toggle(self):
        a,_=self.make_control()
        a.menu.local_scope.presented.return_value=False
        self.assertIsNone(a.selected_slot())
        a.menu.local_scope.presented.return_value=True
        a.session.object_retired.return_value=True
        self.assertIsNone(a.selected_slot())
        a.session.object_retired.return_value=False
        a.slots.collect.return_value={}
        self.assertIsNone(a.selected_slot())

    def test_changed_getter_rejected_without_visibility_writes(self):
        a,_=self.make_control();a.game.u64.return_value=999
        with self.assertRaisesRegex(RuntimeError,'getter changed'):a.selected_slot()
        a.equipment.set_hidden_slots.assert_not_called()

    def test_one_toggle_per_press_and_each_slot_independent(self):
        a,selected=self.make_control()
        def tick(down):return a.handle_button(down,focused=True,device=0)
        tick(False);tick(False)
        self.assertEqual(tick(True),'helmet');self.assertIsNone(tick(True))
        self.assertEqual(a.hidden_slots,{'helmet'})
        tick(False);selected.update({10:False,20:True});tick(True)
        self.assertEqual(a.hidden_slots,{'helmet','boots'})
        tick(False);tick(True)
        self.assertEqual(a.hidden_slots,{'helmet'})

    def test_gameplay_focus_loss_and_reconnect_require_release(self):
        edge=ButtonEdge()
        def tick(down,eligible=True,device=0):return edge.update(down,eligible=eligible,device=device)
        self.assertFalse(tick(True));self.assertFalse(tick(True))
        tick(False);self.assertTrue(tick(True))
        self.assertFalse(tick(True,False));self.assertFalse(tick(True))
        tick(False);self.assertTrue(tick(True))
        self.assertFalse(tick(True,device=1));self.assertFalse(tick(True,device=1))
        tick(False,device=1);self.assertTrue(tick(True,device=1))

    def test_no_slot_read_for_normal_gameplay_or_unfocused_input(self):
        a,_=self.make_control();a.selected_slot=Mock()
        a.menu.names=()
        for down in (False,False,True):a.handle_button(down,focused=True,device=0)
        a.menu.names=('MiniInventory',)
        for down in (False,False,True):a.handle_button(down,focused=False,device=0)
        a.selected_slot.assert_not_called()

    def test_helper_option_can_disable_shortcut(self):
        self.assertNotIn('--no-inventory-armor-toggle',helper_arguments(DEFAULTS))
        self.assertIn('--no-inventory-armor-toggle',helper_arguments(dict(DEFAULTS,inventory_armor_toggle=False)))


if __name__=='__main__':unittest.main()
