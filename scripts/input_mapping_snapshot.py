"""Bounded, coherent reads of engine-owned Enhanced Input arrays."""
import struct


class MappingRebuilt(RuntimeError):
    """The engine replaced an input array while it was being inspected."""


def mapping_snapshot(game, header, stride=0x50):
    for _ in range(3):
        descriptor = game.read(header, 16)
        pointer, count, capacity = struct.unpack('<Qii', descriptor)
        if not 0 <= count <= capacity <= 1024 or (count and not pointer):
            raise RuntimeError('Invalid native input mapping array')
        try:
            raw = game.read(pointer, count*stride) if count else b''
        except OSError:
            if game.read(header, 16) != descriptor:
                continue
            raise
        if game.read(header, 16) == descriptor:
            return descriptor, raw
    raise MappingRebuilt('Input mappings rebuilt during discovery; retry next frame')


def checked_row(game, header, descriptor, row, expected):
    if game.read(header, 16) != descriptor:
        raise MappingRebuilt('Input array changed before capturing a row')
    try:
        raw = game.read(row, 0x50)
    except OSError:
        if game.read(header, 16) != descriptor:
            raise MappingRebuilt('Input array changed while capturing a row') from None
        raise
    # +40 is transient trigger bookkeeping. All action/array/key/cache identity
    # must still match the snapshot that selected this row for remapping.
    if raw[:0x40]+raw[0x41:] != expected[:0x40]+expected[0x41:]:
        raise MappingRebuilt('Input row changed before capture')
    return raw
