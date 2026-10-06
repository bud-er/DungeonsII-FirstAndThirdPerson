"""Read the local display component's native armor-slot component lists.

Only the visual map is read. Inventory, equipment, shared assets, cosmetics
entitlements and saved characters are never changed. Slot names come from the
game's actual mapping, not a guess based on meshes, names or skeleton bones.
"""
import struct
from native_layout import matches

SLOT_TAGS = {f'SW.ItemSlot.Equipment.Armor.{tag}': slot for tag, slot in (
    ('Helmet', 'helmet'), ('Chest', 'chest'), ('Leggings', 'pants'), ('Boots', 'boots'))}
SLOTS = frozenset(SLOT_TAGS.values())
MAP_OFFSET = 0x1F8
GUARDS = {
    0x6192474: bytes.fromhex('498b97f8010000'),  # Display's tag -> components map.
    0x6192480: bytes.fromhex('4963c848c1e10548390411'),  # 32-byte rows, FName key.
    0x6192559: bytes.fromhex('488b5a0848634210488d3cc3'),  # Array at +8, count +10.
    0x6192570: bytes.fromhex('0fb6557741b002488b0be811eaa8fd'),  # Native SetVisibility.
}


def sparse_indices(header, read):
    """Decode a bounded native sparse map, including holes and heap bitsets."""
    if len(header) != 80:
        raise RuntimeError('Invalid armor display map header')
    pointer, count, capacity = struct.unpack_from('<Qii', header)
    bit_pointer, bits, bit_capacity = struct.unpack_from('<Qii', header, 32)
    first_free, free_count = struct.unpack_from('<ii', header, 48)
    if (not 0 <= count <= capacity <= 256 or bits != count or not bits <= bit_capacity <= 1024
            or not 0 <= free_count <= count or (count and pointer < 0x10000)
            or (free_count and not 0 <= first_free < count)
            or (not free_count and first_free != -1)):
        raise RuntimeError('Invalid armor display sparse-map bounds')
    size = ((bits+31)//32)*4
    if bit_pointer:
        flags = read(bit_pointer, size) if size else b''
    else:
        if size > 16:
            raise RuntimeError('Missing armor display allocation flags')
        flags = header[16:16+size]
    indices = [i for i in range(count) if flags[i//8] & (1 << (i%8))]
    if len(indices) != count-free_count:
        raise RuntimeError('Armor display allocation count changed')
    return pointer, count, indices, flags


class ArmorSlots:
    def __init__(self, session):
        self.session, self.game = session, session.game
        s = session
        s.ensure_layout('EquipmentDisplayComponent')
        if s.layouts['EquipmentDisplayComponent']['size'] != 0x310:
            raise RuntimeError('Unsupported armor display component layout')
        for address, expected in GUARDS.items():
            if not matches(self.game, address, expected):
                raise RuntimeError('Unsupported armor slot visual mapping')
        candidates = [o['address'] for o in s.objects if not o['flags'] & 0x30
                      and o['outer'] == s.pawn and s.is_a(o['class'], 'EquipmentDisplayComponent')]
        if len(candidates) != 1:
            raise RuntimeError('Expected one local armor display component')
        self.component = candidates[0]
        self.identity = s.identity(self.component)
        self.pawn_identity = s.identity(s.pawn)

    def collect(self, hidden_slots):
        if not set(hidden_slots) <= SLOTS:
            raise ValueError('Unknown armor visibility slot')
        s, g = self.session, self.game
        if (s.identity(s.pawn) != self.pawn_identity or s.identity(self.component) != self.identity
                or s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn
                or g.u64(self.component+0x20) != s.pawn):
            raise RuntimeError('Local armor display owner changed')
        address = self.component+MAP_OFFSET
        header = g.read(address, 80)
        pointer, count, indices, flags = sparse_indices(header, g.read)
        raw = g.read(pointer, count*32) if count else b''
        result, arrays, seen_slots = {}, [], set()
        for index in indices:
            row = raw[index*32:(index+1)*32]
            name = s.reflection.name(*struct.unpack_from('<II', row))
            slot = SLOT_TAGS.get(name)
            if slot is None:
                continue
            if slot in seen_slots:
                raise RuntimeError('Duplicate native armor display slot')
            seen_slots.add(slot)
            elements, n, capacity = struct.unpack_from('<Qii', row, 8)
            if not 0 <= n <= capacity <= 64 or (n and elements < 0x10000):
                raise RuntimeError('Invalid armor visual component list')
            data = g.read(elements, n*8) if n else b''
            arrays.append((elements, data))
            for component, in struct.iter_unpack('<Q', data):
                if component < 0x10000 or component in result:
                    raise RuntimeError('Ambiguous armor visual ownership')
                result[component] = slot
        if (g.read(address, 80) != header or (count and g.read(pointer, count*32) != raw)
                or sparse_indices(header, g.read)[3] != flags
                or any(data and g.read(pointer, len(data)) != data for pointer, data in arrays)):
            raise RuntimeError('Armor visual mapping changed during inspection')
        return {obj: slot for obj, slot in result.items() if slot in hidden_slots}
