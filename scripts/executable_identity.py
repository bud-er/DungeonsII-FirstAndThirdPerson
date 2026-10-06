"""Read-only identity of the validated game, including certificate-only variants.

This is deliberately narrower than a PE/Authenticode image digest. Every byte
before the certificate table is retained, except the checksum and certificate
directory. In particular, startup headers, imports, resource/debug information,
section padding and native code must still match the pinned game byte for byte.
It establishes binary compatibility, not publisher trust or a malware verdict.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import struct

from build_info import GAME_SHA256


MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_CERTIFICATE_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

# Independently measured from the full-SHA256-pinned Steam build 25647713.
# Its nine sections end at 211154432; the 10080-byte certificate occupies the
# complete tail of the 211164512-byte file. Only PE checksum/security-directory
# fields are zeroed to obtain this digest; no original game bytes are shipped.
REFERENCE_FULL_SHA256 = '231147bd0c655a4ae73f90873675d42917f2bfb3a9ee164fc64f217d6d6bd4ef'
REFERENCE_CANONICAL_SHA256 = '411ed1f2077e079bda526180406e1850a50b97f6425cb70b1c990095a9559a3e'
REFERENCE_PAYLOAD_SIZE = 211154432


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _digest(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError(f'{label} must be a SHA256')
    return value.lower()


def _read_at(stream, offset, length, file_size):
    if offset < 0 or length < 0 or offset + length > file_size:
        raise ValueError('PE data extends beyond the executable')
    stream.seek(offset)
    data = stream.read(length)
    if len(data) != length:
        raise ValueError('Executable changed or was truncated while being read')
    return data


def _power_of_two(value):
    return value > 0 and value & (value - 1) == 0


def _pe_payload(stream, size):
    """Require the conservative PE shape needed for certificate-only matching."""
    dos = _read_at(stream, 0, 64, size)
    if dos[:2] != b'MZ':
        raise ValueError('Not a Windows PE executable')
    pe = struct.unpack_from('<I', dos, 0x3C)[0]
    if not 64 <= pe <= MAX_HEADER_BYTES - 264:
        raise ValueError('PE header location is outside the bounded header')
    coff = _read_at(stream, pe, 24, size)
    if coff[:4] != b'PE\0\0':
        raise ValueError('Invalid PE signature')
    machine, count, _, _, _, optional_size, _ = struct.unpack_from('<HHIIIHH', coff, 4)
    if machine != 0x8664 or not 1 <= count <= 96 or optional_size != 240:
        raise ValueError('Certificate equivalence requires the supported x64 PE32+ layout')
    optional = pe + 24
    header = _read_at(stream, optional, optional_size, size)
    if struct.unpack_from('<H', header)[0] != 0x20B or struct.unpack_from('<I', header, 108)[0] != 16:
        raise ValueError('Unsupported PE optional-header layout')
    section_alignment, file_alignment = struct.unpack_from('<II', header, 32)
    image_size, header_size = struct.unpack_from('<II', header, 56)
    if (not _power_of_two(file_alignment) or not 512 <= file_alignment <= 65536
            or not _power_of_two(section_alignment) or section_alignment < max(4096, file_alignment)
            or not image_size or image_size % section_alignment
            or not 0 < header_size <= min(size, MAX_HEADER_BYTES)
            or header_size % file_alignment):
        raise ValueError('Invalid PE image/header alignment')
    table_start = optional + optional_size
    table_end = table_start + count * 40
    if table_end > header_size:
        raise ValueError('PE section table extends beyond the headers')
    table = _read_at(stream, table_start, count * 40, size)
    raw_ranges, mapped_ranges = [], []
    last_rva = 0
    for index in range(count):
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', table, index * 40 + 8)
        if not rva or rva % section_alignment or rva <= last_rva:
            raise ValueError('Invalid PE section address/order')
        last_rva = rva
        mapped_size = max(virtual_size, raw_size)
        if not mapped_size or rva < header_size or rva + mapped_size > image_size:
            raise ValueError('Invalid PE mapped-section extent')
        mapped_ranges.append((rva, rva + mapped_size))
        if raw_size:
            if (raw_offset < header_size or raw_offset % file_alignment
                    or raw_size % file_alignment or raw_offset + raw_size > size):
                raise ValueError('Invalid PE raw-section extent/alignment')
            raw_ranges.append((raw_offset, raw_offset + raw_size))
        elif raw_offset:
            raise ValueError('Empty PE section has an unexpected file offset')
    for ranges in (raw_ranges, mapped_ranges):
        ordered = sorted(ranges)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError('Overlapping PE sections')
    if not raw_ranges:
        raise ValueError('PE has no initialized sections')
    payload_end = max(end for _, end in raw_ranges)
    certificate_offset, certificate_size = struct.unpack_from('<II', header, 144)
    if certificate_offset == certificate_size == 0:
        if size != payload_end:
            raise ValueError('Executable has an unrecognized file overlay')
    else:
        if (certificate_offset != payload_end or certificate_offset % 8
                or not 8 < certificate_size <= MAX_CERTIFICATE_BYTES
                or certificate_size % 8 or certificate_offset + certificate_size != size):
            raise ValueError('Certificate table must be the complete unmapped file tail')
        cursor, entries = certificate_offset, 0
        while cursor < size:
            length, revision, kind = struct.unpack('<IHH', _read_at(stream, cursor, 8, size))
            entries += 1
            if length <= 8 or revision != 0x200 or kind != 2 or entries > 32:
                raise ValueError('Unsupported WIN_CERTIFICATE entry')
            rounded_length = (length + 7) & ~7
            if cursor + rounded_length > size:
                raise ValueError('WIN_CERTIFICATE entry extends beyond its table')
            padding = _read_at(stream, cursor + length, rounded_length - length, size)
            if any(padding):
                raise ValueError('WIN_CERTIFICATE alignment padding is not zero')
            cursor += rounded_length
        if cursor != size:
            raise ValueError('WIN_CERTIFICATE table length is inconsistent')
    return payload_end, ((optional + 64, 4), (optional + 144, 8))


def _hash_stream(stream, length, zero_fields=()):
    digest, offset = hashlib.sha256(), 0
    stream.seek(0)
    while offset < length:
        data = stream.read(min(CHUNK_BYTES, length - offset))
        if not data:
            raise ValueError('Executable changed or was truncated while being hashed')
        if zero_fields:
            data = bytearray(data)
            for start, count in zero_fields:
                left, right = max(offset, start), min(offset + len(data), start + count)
                if left < right:
                    data[left - offset:right - offset] = bytes(right - left)
        digest.update(data)
        offset += len(data)
    return digest.hexdigest()


def identify_executable(path, *, expected_sha256=GAME_SHA256,
                        expected_canonical_sha256=None, expected_payload_size=None):
    """Hash one stable local file; never inspect or modify a running process.

    The original full hash remains the first match. A different expected full
    hash disables the pinned canonical fallback unless the caller explicitly
    supplies both alternate canonical expectations (used by isolated tests).
    Malformed nonmatching PE files return an identity_error and remain blocked.
    File access/race/bounds failures raise OSError/ValueError instead of returning
    a compatibility decision. Returned metadata contains no installation path.
    """
    expected_sha256 = _digest(expected_sha256, 'Expected executable hash')
    if expected_canonical_sha256 is None:
        if expected_payload_size is not None:
            raise ValueError('A payload size requires an explicit canonical hash')
        if expected_sha256 == REFERENCE_FULL_SHA256:
            expected_canonical_sha256 = REFERENCE_CANONICAL_SHA256
            expected_payload_size = REFERENCE_PAYLOAD_SIZE
    else:
        expected_canonical_sha256 = _digest(expected_canonical_sha256, 'Expected canonical hash')
        if type(expected_payload_size) is not int or not 0 < expected_payload_size <= MAX_EXECUTABLE_BYTES:
            raise ValueError('Expected canonical payload size is invalid')
    path = Path(path)
    info = path.stat()
    before_path = _identity(info)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_EXECUTABLE_BYTES:
        raise ValueError('Game executable must be a bounded regular file')
    with path.open('rb') as stream:
        before = _identity(os.fstat(stream.fileno()))
        # Windows stat/fstat ctime semantics differ; match ctime only within
        # each API, while volume/file ID/size/mtime must agree across APIs.
        if before[:4] != before_path[:4]:
            raise ValueError('Executable changed before it was opened')
        digest = _hash_stream(stream, before[2])
        result = {'sha256': digest, 'compatible': digest == expected_sha256,
                  'match_kind': 'exact' if digest == expected_sha256 else None,
                  'canonical_sha256': None, 'payload_size': None, 'size': before[2],
                  'file_identity': dict(zip(('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'), before_path))}
        if not result['compatible'] and expected_canonical_sha256 is not None:
            try:
                payload_size, fields = _pe_payload(stream, before[2])
                canonical = _hash_stream(stream, payload_size, fields)
                result.update(canonical_sha256=canonical, payload_size=payload_size)
                if canonical == expected_canonical_sha256 and payload_size == expected_payload_size:
                    result.update(compatible=True, match_kind='certificate_equivalent')
            except ValueError as error:
                result['identity_error'] = str(error)
        if before != _identity(os.fstat(stream.fileno())) or before_path != _identity(path.stat()):
            raise ValueError('Executable changed during identity validation')
    return result
