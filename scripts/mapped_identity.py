"""Exact read-only fingerprint for the protected Xbox 1.1.1.0 game mapping.

The GDK executable cannot be opened as a file on this installation. The mapped
gameplay/engine code range and full unwind table are fingerprinted before
requesting write access. PE layout and every measured native guard must match.
The trailing runtime code changes in normal gameplay; it is not fingerprinted.
The pinned range contains every native instruction the Helper depends on. This
does not accept other versions, infer compatibility from a name, or scan for
replacement offsets. No game executable is copied or modified.
"""
import hashlib
import struct

from native_layout import GDK

RANGES = (
    (4096, 117436416, '920c7bc3fe22ab147a9eda3259152b4b899e1f45109030ee68a32852aef5eff6'),
    (186433536, 7288260, '88bad43351d18904bb8849557a909e82baa29f55c05d185d8774a5944150213c'),
)
TIMESTAMP = 2168503815
IMAGE_SIZE = 197652480
SECTIONS = (
    (b'.text', 4096, 135449131, 135449600, 1610612768),
    (b'.rdata', 135454720, 43291562, 43291648, 1073741888),
    (b'.data', 178749440, 7680996, 3250176, 3221225536),
    (b'.pdata', 186433536, 7288260, 7288320, 1073741888),
    (b'.msvcjmc', 193724416, 8, 512, 3221225536),
    (b'_RDATA', 193728512, 383248, 383488, 1073741888),
    (b'.xbld', 194113536, 452, 512, 3254779968),
    (b'.rsrc', 194117632, 9552, 9728, 1073741888),
    (b'.reloc', 194129920, 3520204, 3520512, 1107296320),
)


def metadata(header):
    if len(header) != 4096 or header[:2] != b'MZ':
        raise ValueError('Unsupported mapped DOS header')
    nt = struct.unpack_from('<I', header, 0x3C)[0]
    if not 64 <= nt <= 2048 or header[nt:nt+4] != b'PE\0\0':
        raise ValueError('Unsupported mapped NT header')
    machine, count, stamp = struct.unpack_from('<HHI', header, nt+4)
    optional_size = struct.unpack_from('<H', header, nt+20)[0]
    opt = nt+24
    magic = struct.unpack_from('<H', header, opt)[0]
    image_size = struct.unpack_from('<I', header, opt+56)[0]
    if (machine, count, stamp, optional_size, magic, image_size) != (
            0x8664, len(SECTIONS), TIMESTAMP, 240, 0x20B, IMAGE_SIZE):
        raise ValueError('Unknown Xbox game build')
    sections = []
    for i, expected in enumerate(SECTIONS):
        start = opt+optional_size+i*40
        row = header[start:start+40]
        name = row[:8].rstrip(b'\0')
        size, rva, raw_size = struct.unpack_from('<III', row, 8)
        flags = struct.unpack_from('<I', row, 36)[0]
        if (name, rva, size, raw_size, flags) != expected:
            raise ValueError('Unknown Xbox mapped section layout')
        sections.append(dict(rva=rva, virtual_size=size, raw_size=raw_size, characteristics=flags))
    return dict(machine=machine, optional_magic=magic, timestamp=stamp,
                image_size=image_size, entrypoint_rva=struct.unpack_from('<I', header, opt+16)[0],
                subsystem=struct.unpack_from('<H', header, opt+68)[0],
                section_count=count, sections=sections)


def identify_mapped_gdk(reader):
    """Caller must own a read-only, identity-checked handle to the actual image."""
    header = reader.read(reader.base, 4096)
    pe = metadata(header)
    for start, size, expected_digest in RANGES:
        hasher = hashlib.sha256()
        for offset in range(0, size, 1024*1024):
            hasher.update(reader.read(reader.base+start+offset, min(1024*1024, size-offset)))
        if hasher.hexdigest() != expected_digest:
            raise ValueError('Xbox executable code/layout differs from the measured build')
    from gdk_layout import ADDRESSES, GUARDS
    for source, (_, expected) in GUARDS.items():
        if reader.read(reader.base+ADDRESSES[source], len(expected)) != expected:
            raise ValueError('Xbox native layout guard differs from the measured build')
    if reader.read(reader.base, 4096) != header:
        raise ValueError('Xbox image changed during identification')
    return dict(profile=GDK, match_kind='mapped_code',
                mapped_code_sha256=RANGES[0][2], unwind_sha256=RANGES[1][2], pe=pe)
