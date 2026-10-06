"""Read actual local UI activation; keys never decide whether a menu is open."""
from native_layout import matches as native_matches, rva as native_rva

import struct
import time

from inspect_camera import OBJECT_ARRAY_RVA
from local_menu_scope import LocalMenuScope, MenuPresentationChanging


VILLAGER_MENUS = frozenset(('W_QuestDialogue_Activatable', 'W_QuestAcceptance_Activatable',
                          'W_VillageMerchant_Activatable',
                          'W_VillageMerchantPurchasePreview_Activatable',
                          'W_Enchantsmith_Activatable'))
KEEP_FIRST_PERSON = VILLAGER_MENUS | {'MiniInventory'}


class LocalUICapture:
    """Read the local controller's actual UI handoff; never invoke native code.

    On the fingerprinted Build25647713, reflected IsInputCapturedByUI calls
    this getter: movzx eax, byte ptr [rcx+0x984]; ret. A surviving widget's
    bIsActive/Visibility can outlive its participation in the local UI. This
    flag is positive evidence only: some real menus leave it false, so their
    local ownership and presentation are checked independently.
    """
    GETTER_RVA = 0x6382230
    GETTER_BYTES = bytes.fromhex('0F B6 81 84 09 00 00 C3')
    OFFSET = 0x984

    def __init__(self, game):
        self.game = game
        if not native_matches(game, self.GETTER_RVA, self.GETTER_BYTES):
            raise RuntimeError('Unsupported local UI-capture getter')

    def captured(self, controller):
        value = self.game.read(controller+self.OFFSET, 1)[0]
        if value not in (0, 1):
            raise RuntimeError('Unknown local UI-capture state')
        return bool(value)


def keeps_first_person(name):
    # Retain the active custom perspective (first OR shoulder) while releasing
    # input to these local overlays. Full inventory/pause/cinematics still exit.
    return name.split('_C_')[0] in KEEP_FIRST_PERSON


def is_villager_menu(name):
    return name.split('_C_')[0] in VILLAGER_MENUS


def captures_menu(active, mode, visibility):
    # Verified ESpicewoodWidgetInputMode: Default=0, GameAndMenu=1,
    # Game=2, Menu=3. HUD uses 0/1. Inventory/navigation/wheels use 3.
    if mode not in (0, 1, 2, 3):
        raise RuntimeError('Unknown widget input mode')
    return active and mode == 3 and visibility not in (1, 2)


class MenuState:
    def __init__(self, session):
        self.session, self.game = session, session.game
        for name in ('CommonActivatableWidget', 'SpicewoodActivatableWidget', 'Widget'):
            session.ensure_layout(name)
        fields = {p['name']: p for p in session.layouts['SpicewoodActivatableWidget']['properties']}
        if fields['InputConfig'].get('target') != 'ESpicewoodWidgetInputMode' or fields['InputConfig']['size'] != 1:
            raise RuntimeError('Unknown UI input-mode layout')
        self.local_capture = LocalUICapture(self.game)
        self.local_scope = LocalMenuScope(session)
        self.seen = {o['index']: (o['address'], o['serial']) for o in session.objects}
        self.widgets = {}
        self.armor_widgets = {}
        # Share the incremental object walk with local presentation. Full
        # rescans while moving would stall the camera/animation updates.
        self.characters = {}
        self.players = {}
        self.cursor, self.next_read, self.names = 0, 0., ()
        self.input_captured = False
        for obj in session.objects:
            try:
                self.consider(obj)
            except (OSError,RuntimeError,ValueError):
                pass  # A live mob/widget can retire during the initial snapshot.

    def consider(self, obj):
        s = self.session
        if obj['flags'] & 0x30:
            return
        self.local_scope.consider(obj)
        address = obj['address']
        if 'AS_EquippedItemSlot' in getattr(s, 'native', {}) and s.is_a(obj['class'], 'AS_EquippedItemSlot'):
            s.ensure_layout('AS_EquippedItemSlot')
            tag = s.field(address, 'AS_EquippedItemSlot', 'EquipmentSlotTag')
            if tag['type'] != 'StructProperty' or tag.get('target') != 'GameplayTag' or tag['size'] != 8:
                raise RuntimeError('Unsupported equipped slot tag')
            self.armor_widgets[address] = (s.identity(address), tag['address'])
        if s.is_a(obj['class'], 'PlayerCharacter'):
            self.players[address] = s.identity(address)
        if s.is_a(obj['class'],'MobCharacter') and not s.is_a(obj['class'],'PlayerCharacter'):
            self.characters[address] = s.identity(address)
        if not s.is_a(obj['class'], 'SpicewoodActivatableWidget'):
            return
        active = s.field(address, 'CommonActivatableWidget', 'bIsActive')
        mode = s.field(address, 'SpicewoodActivatableWidget', 'InputConfig')
        visible = s.field(address, 'Widget', 'Visibility')
        self.widgets[address] = (s.identity(address), obj['name'], active['address'],
                                 active['bool']['byteMask'], mode['address'], visible['address'])

    def advance_discovery(self):
        # Bound discovery work per camera tick. Never rescan/read every UObject
        # synchronously on menu close, which would stall the visibility updates.
        g, s = self.game, self.session
        array = g.read(g.base+native_rva(g, OBJECT_ARRAY_RVA), 0x30)
        chunks = struct.unpack_from('<Q', array, 0x10)[0]
        maximum, count = struct.unpack_from('<II', array, 0x20)
        if not 0 < count <= maximum < 10_000_000:
            raise RuntimeError('Invalid UI object array')
        if self.cursor >= count:
            self.cursor = 0
        chunk_index, offset = divmod(self.cursor, 65536)
        chunk = g.u64(chunks+chunk_index*8)
        length = min(8192, 65536-offset, count-self.cursor)
        data = g.read(chunk+offset*24, length*24)
        until = time.perf_counter()+.001
        for i in range(length):
            index = self.cursor
            self.cursor += 1
            pointer = struct.unpack_from('<Q', data, i*24)[0]
            serial = struct.unpack_from('<I', data, i*24+0x10)[0]
            token = (pointer, serial)
            if self.seen.get(index) != token:
                old = self.seen.get(index)
                self.seen[index] = token
                if old:
                    self.widgets.pop(old[0], None)
                    self.armor_widgets.pop(old[0], None)
                    self.characters.pop(old[0], None)
                    self.players.pop(old[0], None)
                    self.local_scope.retire(old[0])
                    s.reflection.headers.pop(old[0], None)
                    s.ancestors.pop(old[0], None)
                if pointer:
                    s.reflection.headers.pop(pointer, None)
                    try:
                        self.consider(s.reflection.header(pointer))
                    except (OSError, RuntimeError, ValueError, UnicodeError):
                        pass  # An object can disappear during discovery.
            if time.perf_counter() >= until:
                break

    def read_names(self):
        s, g = self.session, self.game
        names = []
        # Read known stacks directly so newly displayed menu instances do not
        # wait for the incremental global object walk to reach them.
        for obj in self.local_scope.begin():
            if obj not in self.widgets:
                s.reflection.headers.pop(obj, None)
                self.consider(s.reflection.header(obj))
        for obj, (identity, name, active, mask, mode, visible) in list(self.widgets.items()):
            try:
                if s.identity(obj) != identity:
                    self.widgets.pop(obj)
                    continue
                if captures_menu(bool(g.read(active, 1)[0] & mask), g.read(mode, 1)[0], g.read(visible, 1)[0]) and \
                        self.local_scope.presented(obj, identity):
                    names.append(name)
            except (OSError, RuntimeError, ValueError):
                if s.object_retired(obj, identity):
                    self.widgets.pop(obj, None)
                else:
                    raise  # Unknown live UI state must not recapture input.
        return tuple(names)

    def reason(self):
        s, g = self.session, self.game
        if s.identity(s.pc) != s.identities[s.pc] or s.identity(s.pawn) != s.identities[s.pawn] \
                or s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn:
            raise RuntimeError('Local player changed; press F6 in the new playable area')
        self.advance_discovery()
        now = time.monotonic()
        # Some menus never set the native capture flag. Accept either positive
        # capture or a menu proven present in this player's live UI graph.
        captured = self.local_capture.captured(s.pc)
        changing = False
        if now >= self.next_read:
            try:
                self.names = self.read_names()
                self.next_read = now+1/30
            except MenuPresentationChanging:
                # A normal animation/stack transition is not a camera fault.
                # Release input and retry next tick without changing the last
                # proven camera classification or restoring the whole session.
                changing = True
                self.next_read = 0.
        self.input_captured = captured or bool(self.names) or changing
        if any(not keeps_first_person(name) for name in self.names):
            return 'menu'
        if s.pointer(s.pawn, 'PlayerCharacter', 'CurrentCamera') != s.camera:
            return 'game camera'
        view_target = getattr(s, 'view_target_profile', None)
        if view_target is not None:
            return view_target.reason()
        return None
