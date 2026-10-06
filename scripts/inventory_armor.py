"""Local inventory selection and reversible armor presentation, without item edits."""
import struct
from types import SimpleNamespace

from armor_slots import ArmorSlots, SLOTS, SLOT_TAGS
from equipment_profile import EquipmentProfile


class ButtonEdge:
    """Require release after focus/eligibility/device changes; never repeat a hold."""
    def __init__(self):
        self.device = None
        self.armed = False
        self.down = False

    def update(self, down, *, eligible, device):
        if not eligible or device is None or device != self.device:
            self.device, self.armed, self.down = device, False, bool(down)
            return False
        pressed = bool(down) and not self.down and self.armed
        self.down = bool(down)
        if not down:
            self.armed = True
        return pressed


class InventoryArmor:
    # Measured CommonButtonBase.GetSelected: native wrapper, reading bit 0 at
    # +4C1. Resolve the reflected function in each pinned build; no native call.
    SELECTED_GETTER = bytes.fromhex(
        '488b42204533c94885c0410f95c14c03c84c894a200fb681c10400002401418800c3')

    def __init__(self, menu, hidden_slots=()):
        self.menu, self.session = menu, menu.session
        self.game = self.session.game
        self.edge = ButtonEdge()
        self.hidden_slots = frozenset(hidden_slots)
        if not self.hidden_slots <= SLOTS:
            raise ValueError('Unknown armor visibility slot')
        s = self.session
        s.ensure_layout('Character')
        s.ensure_layout('AS_EquippedItemSlot')
        s.ensure_layout('SkinnedMeshComponent')
        s.ensure_layout('SpicewoodButtonBase')
        functions = [o for o in s.objects if o['name'] == 'GetSelected'
                     and s.reflection.header(o['class'])['name'] == 'Function'
                     and s.reflection.header(o['outer'])['name'] == 'CommonButtonBase']
        if len(functions) != 1:
            raise RuntimeError('Inventory selection getter is unavailable')
        function = functions[0]['address']
        self.function_identity = s.identity(function)
        self.function = function
        self.getter = self.game.u64(function+0xD8)
        if self.game.read(self.getter,len(self.SELECTED_GETTER)) != self.SELECTED_GETTER:
            raise RuntimeError('Unsupported inventory selection getter')
        self.slots = ArmorSlots(s)
        self.mesh = s.pointer(s.pawn,'Character','Mesh')
        self.asset = s.pointer(self.mesh,'SkinnedMeshComponent','SkinnedAsset')
        s.remember(self.mesh,'SkeletalMeshComponent')
        s.remember(self.asset,'SkeletalMesh')
        if s.reflection.header(self.asset)['name'] != 'SK_Player_Master':
            raise RuntimeError('Unsupported inventory armor skeleton')
        s.hidden_armor_slots = self.hidden_slots
        avatar = SimpleNamespace(session=s, game=self.game, mesh=self.mesh, asset=self.asset)
        self.equipment = EquipmentProfile(avatar, hide_body=False)
        self.equipment.shared = True

    def validate(self):
        s = self.session
        s.local_scope.validate()
        if (s.pointer(s.pawn,'Character','Mesh') != self.mesh or
                s.pointer(self.mesh,'SkinnedMeshComponent','SkinnedAsset') != self.asset or
                s.identity(self.mesh) != s.identities[self.mesh] or
                s.identity(self.asset) != s.identities[self.asset]):
            raise RuntimeError('Armor presentation character changed')

    def selected_slot(self):
        self.validate()
        s, g = self.session, self.game
        if (s.identity(self.function) != self.function_identity or
                g.u64(self.function+0xD8) != self.getter or
                g.read(self.getter,len(self.SELECTED_GETTER)) != self.SELECTED_GETTER):
            raise RuntimeError('Inventory selection getter changed')
        # Reuse the menu's bounded incremental object discovery; no world scan.
        selected = []
        for obj, (identity, tag_address) in self.menu.armor_widgets.items():
            if s.object_retired(obj,identity):
                continue
            if not g.read(obj+0x4C1,1)[0] & 1:
                continue
            tag = s.reflection.name(*struct.unpack('<II',g.read(tag_address,8)))
            slot = SLOT_TAGS.get(tag)
            if slot is None or not self.menu.local_scope.presented(obj,identity):
                continue
            if s.identity(obj) != identity or not g.read(obj+0x4C1,1)[0] & 1:
                return None
            selected.append(slot)
        if len(selected) != 1:
            return None
        slot = selected[0]
        # Empty slots cannot set a hidden preference for a future item.
        return slot if self.slots.collect({slot}) else None

    def handle_button(self, down, *, focused, device):
        eligible = bool(focused and self.menu.input_captured and self.menu.names)
        if not self.edge.update(down,eligible=eligible,device=device):
            return None
        slot = self.selected_slot()
        if slot is None:
            return None
        self.hidden_slots = self.hidden_slots ^ {slot}
        self.equipment.set_hidden_slots(self.hidden_slots)
        return slot

    def bind(self, camera_session):
        self.validate()
        if camera_session.pawn != self.session.pawn:
            raise RuntimeError('Camera and armor belong to different characters')
        camera_session.shared_equipment = self.equipment
        camera_session.hidden_armor_slots = self.hidden_slots

    def update_native_view(self):
        self.validate()
        self.equipment.update(hide_body=False)

    def restore(self):
        self.equipment.restore()
        if self.equipment.restore_failures:
            raise RuntimeError('Armor visibility restoration incomplete')
