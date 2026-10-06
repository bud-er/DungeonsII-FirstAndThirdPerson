"""Read-only proof that a menu is presented for this local player.

An active UObject can survive removal from a CommonUI stack. Follow the actual
displayed widget, panel slots and widget-tree roots to a live viewport entry.
The native guards below were derived from Build25647713's owning-player getter
and GameViewportSubsystem::IsWidgetAdded; no native function is called.
"""
import struct


OWNER_GETTER_RVA = 0x373B660
OWNER_GETTER = bytes.fromhex(
    '40534883ec20488d9928030000488bcbe86b78810084c0740d488bcb4883c4205b'
    'e91afa800033c04883c4205bc3')
GUARDS = {
    OWNER_GETTER_RVA: OWNER_GETTER,
    0x3F4B0A0: bytes.fromhex('e96b1f77fd'),
    0x36646D4: bytes.fromhex('488b56600f1f8400000000004963c0486bc878'),
    0x3664732: bytes.fromhex('483958500f8476feffff488b40584885c00f8469feffff8b400885c0'),
}


def weak_token(identity):
    return identity[0][:4] + identity[1]


class RetiredWidget(RuntimeError):
    pass


class MenuPresentationChanging(RuntimeError):
    """A coherent UI snapshot was not available during a native transition."""


class LocalMenuScope:
    MAX_NODES = 4096
    MAX_DEPTH = 64

    def __init__(self, session):
        self.session, self.game = session, session.game
        for name in ('UserWidget', 'WidgetTree', 'PanelSlot', 'PanelWidget',
                     'CommonActivatableWidgetContainerBase', 'Level'):
            session.ensure_layout(name)
        for rva, expected in GUARDS.items():
            if self.game.read(self.game.base+rva, len(expected)) != expected:
                raise RuntimeError('Unsupported local menu ownership/viewport layout')
        self.player = session.pointer(session.pc, 'PlayerController', 'Player')
        if not self.player or not session.is_a(session.reflection.header(self.player)['class'], 'LocalPlayer'):
            raise RuntimeError('Menu input requires the local player')
        self.player_identity = session.identity(self.player)
        self.player_token = weak_token(self.player_identity)
        level = session.reflection.header(session.pc)['outer']
        if not level or not session.is_a(session.reflection.header(level)['class'], 'Level'):
            raise RuntimeError('Local menu controller has no level')
        self.world = session.pointer(level, 'Level', 'OwningWorld')
        self.world_identity = session.identity(self.world)
        self.world_token = weak_token(self.world_identity)
        self.containers, self.viewports = {}, {}
        self.displayed, self.viewport_entries = {}, None

    def consider(self, obj):
        if obj['flags'] & 0x30:
            return
        for cls, records in (('CommonActivatableWidgetContainerBase', self.containers),
                             ('GameViewportSubsystem', self.viewports)):
            if self.session.is_a(obj['class'], cls):
                records[obj['address']] = self.session.identity(obj['address'])

    def retire(self, address):
        self.containers.pop(address, None)
        self.viewports.pop(address, None)

    def _live(self, obj, identity):
        s = self.session
        if s.object_retired(obj, identity) or s.identity(obj) != identity:
            raise RetiredWidget('Menu object retired')

    def begin(self):
        s = self.session
        if s.pointer(s.pc, 'PlayerController', 'Player') != self.player or \
                s.identity(self.player) != self.player_identity or s.identity(self.world) != self.world_identity:
            raise RuntimeError('Local menu player/world changed')
        self.displayed, self.viewport_entries = {}, None
        for obj, identity in list(self.containers.items()):
            try:
                self._live(obj, identity)
                target = s.pointer(obj, 'CommonActivatableWidgetContainerBase', 'DisplayedWidget')
                if target:
                    self.displayed.setdefault(target, []).append(obj)
            except RetiredWidget:
                self.retire(obj)
        return tuple(self.displayed)

    def _viewport(self):
        if self.viewport_entries is not None:
            return self.viewport_entries
        entries = {}
        for obj, identity in list(self.viewports.items()):
            try:
                self._live(obj, identity)
            except RetiredWidget:
                self.retire(obj)
                continue
            # Native IsWidgetAdded uses a weak UObject key and a 0x78-byte
            # sparse-map node. Free slots must not be mistaken for live UI.
            for attempt in range(2):
                raw = self.game.read(obj+0x60, 0x50)
                data, count, capacity = struct.unpack_from('<Qii', raw)
                bits = struct.unpack_from('<Q', raw, 0x20)[0] or obj+0x70
                bit_count, bit_capacity, _, free = struct.unpack_from('<iiii', raw, 0x28)
                if not (0 <= free <= count <= capacity <= self.MAX_NODES and
                        count == bit_count <= bit_capacity <= self.MAX_NODES):
                    raise RuntimeError('Invalid local UI viewport map')
                flags = self.game.read(bits, (count+7)//8) if count else b''
                nodes = self.game.read(data, count*0x78) if count else b''
                if self.game.read(obj+0x60, 0x50) == raw and \
                        (not count or self.game.read(bits, (count+7)//8) == flags and
                         self.game.read(data, count*0x78) == nodes):
                    break
            else:
                raise MenuPresentationChanging('Local UI viewport map changed during read')
            used = 0
            for index in range(count):
                if not flags[index//8] & (1 << (index%8)):
                    continue
                used += 1
                node = nodes[index*0x78:(index+1)*0x78]
                # Map value starts at node+8. Native IsWidgetAdded checks its
                # Slate pointer at value+0x50 and shared control at value+0x58.
                slate, control = struct.unpack_from('<QQ', node, 0x58)
                entries[node[:8]] = (slate, control)
            if used != count-free:
                raise MenuPresentationChanging('Local UI viewport allocation changed during read')
            self._live(obj, identity)
        if not self.viewports:
            raise RuntimeError('Local UI viewport subsystem is unavailable')
        self.viewport_entries = entries
        return entries

    def presented(self, obj, expected):
        s, g = self.session, self.game
        identities, reads, visiting, references = {}, {}, set(), set()

        def read(address, size):
            key = address, size
            if key not in reads:
                reads[key] = g.read(address, size)
            return reads[key]

        def pointer(address, cls, name):
            field = s.field(address, cls, name)
            if field['type'] != 'ObjectProperty' or field['size'] != 8:
                raise RuntimeError('Unsupported menu link layout')
            return struct.unpack('<Q', read(field['address'], 8))[0]

        def header(address, cls):
            identity = identities.setdefault(address, s.identity(address))
            self._live(address, identity)
            s.reflection.headers.pop(address, None)
            value = s.reflection.header(address)
            if value['flags'] & 0x30 or not s.is_a(value['class'], cls):
                raise RetiredWidget('Menu link no longer names a live widget')
            return value

        def walk(address, depth):
            if depth >= self.MAX_DEPTH or address in visiting:
                raise RuntimeError('Cyclic or excessive menu parent chain')
            visiting.add(address)
            try:
                h = header(address, 'Widget')
                visibility = s.field(address, 'Widget', 'Visibility')
                visible = read(visibility['address'], 1)[0]
                if visible not in range(5):
                    raise RuntimeError('Unknown parent widget visibility')
                if visible in (1, 2):
                    return False
                if s.is_a(h['class'], 'UserWidget'):
                    if g.u64(g.u64(address)+0x2E0) != g.base+OWNER_GETTER_RVA:
                        raise RuntimeError('Local widget owner getter changed')
                    context = read(address+0x328, 16)
                    if context[:8] != self.player_token or context[8:] not in (bytes(8), self.world_token):
                        return False
                entry = self._viewport().get(weak_token(identities[address]))
                if entry is not None and read(address+0xD9, 1)[0] & 0x20:
                    slate, control = entry
                    if not slate or not control:
                        return False
                    references.add(control)
                    return struct.unpack('<i', g.read(control+8, 4))[0] > 0
                slot = pointer(address, 'Widget', 'Slot')
                if slot:
                    header(slot, 'PanelSlot')
                    if pointer(slot, 'PanelSlot', 'Content') != address:
                        return False
                    parent = pointer(slot, 'PanelSlot', 'Parent')
                    return bool(parent and walk(parent, depth+1))
                # Stack children have no panel slot. Being in WidgetList (or
                # merely sharing a WidgetTree) does not mean they are shown.
                for container in self.displayed.get(address, ()):
                    if pointer(container, 'CommonActivatableWidgetContainerBase', 'DisplayedWidget') == address and walk(container, depth+1):
                        return True
                outer = h['outer']
                if outer:
                    oh = s.reflection.header(outer)
                    if s.is_a(oh['class'], 'WidgetTree'):
                        header(outer, 'WidgetTree')
                        owner = oh['outer']
                        if owner and pointer(outer, 'WidgetTree', 'RootWidget') == address and \
                                pointer(owner, 'UserWidget', 'WidgetTree') == outer:
                            return walk(owner, depth+1)
                return False
            finally:
                visiting.remove(address)

        try:
            self._live(obj, expected)
            result = walk(obj, 0)
            for address, identity in identities.items():
                self._live(address, identity)
            if any(g.read(address, size) != value for (address, size), value in reads.items()):
                raise MenuPresentationChanging('Local menu presentation changed during read')
            # A Slate tick can change a positive shared count harmlessly.
            # Only loss of the last live reference invalidates presentation.
            if result and any(struct.unpack('<i', g.read(control+8, 4))[0] <= 0 for control in references):
                raise MenuPresentationChanging('Local menu viewport reference retired during read')
            return result
        except RetiredWidget:
            return False
