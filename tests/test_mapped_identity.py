from pathlib import Path
import hashlib
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mapped_identity as identity
import native_layout


def header():
    raw = bytearray(4096)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 128)
    raw[128:132] = b'PE\0\0'
    struct.pack_into('<HHI', raw, 132, 0x8664, 9, identity.TIMESTAMP)
    struct.pack_into('<H', raw, 148, 240)
    opt = 152
    struct.pack_into('<H', raw, opt, 0x20B)
    struct.pack_into('<I', raw, opt+56, identity.IMAGE_SIZE)
    for i, (name, rva, size, raw_size, flags) in enumerate(identity.SECTIONS):
        start = opt+240+i*40
        raw[start:start+len(name)] = name
        struct.pack_into('<III', raw, start+8, size, rva, raw_size)
        struct.pack_into('<I', raw, start+36, flags)
    return bytes(raw)


class MappedBuildIdentity(unittest.TestCase):
    def verify(self, *, mutation=None, base=0x140000000, header_bytes=None):
        code, unwind = b'fixture measured gameplay code', b'fixture unwind layout'
        ranges = ((0x1000, len(code), hashlib.sha256(code).hexdigest()),
                  (0x8000, len(unwind), hashlib.sha256(unwind).hexdigest()))
        memory = {0: header_bytes or header(), 0x1000: code, 0x8000: unwind, 0x9000: b'guard'}
        if mutation:
            memory.update(mutation)
        reads = []
        def read(address, size):
            reads.append((address-base, size))
            value = memory[address-base]
            if isinstance(value, Exception):
                raise value
            self.assertEqual(len(value), size)
            return value
        reader = SimpleNamespace(base=base, read=read)
        with patch.object(identity, 'RANGES', ranges), \
                patch('gdk_layout.ADDRESSES', {1: 0x9000}), \
                patch('gdk_layout.GUARDS', {1: (b'guard', b'guard')}):
            result = identity.identify_mapped_gdk(reader)
        return result, reads

    def test_aslr_changes_addresses_not_identity_and_never_uses_a_write_api(self):
        first, reads = self.verify()
        second, _ = self.verify(base=0x7FF612340000)
        self.assertEqual(first, second)
        self.assertEqual(first['match_kind'], 'mapped_code')
        self.assertNotIn('sha256', first)  # Not advertised as a full file hash.
        self.assertEqual(reads[0], reads[-1])

    def test_changed_code_unwind_or_guard_is_rejected(self):
        for address, replacement in ((0x1000, b'Xixture measured gameplay code'),
                                      (0x8000, b'Xixture unwind layout'), (0x9000, b'wrong')):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.verify(mutation={address: replacement})

    def test_partial_or_inaccessible_mapping_is_not_accepted(self):
        with self.assertRaises(OSError):
            self.verify(mutation={0x1000: OSError('mapping unavailable')})

    def test_unknown_header_or_sections_are_rejected_before_code_reads(self):
        for offset in (0, 60, 132, 134, 136, 148, 152, 208, 392+12, 392+36):
            raw = bytearray(header());raw[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.verify(header_bytes=bytes(raw))

    def test_changed_header_during_hash_does_not_authorize_build(self):
        original = identity.metadata
        # Header is reread; a self-mutating fixture simulates module replacement.
        calls = 0
        def read(address, size):
            nonlocal calls
            calls += 1
            raw = bytearray(header())
            if calls > 1:raw[2] = 1
            return bytes(raw)
        with patch.object(identity, 'RANGES', ()), patch('gdk_layout.GUARDS', {}):
            with self.assertRaisesRegex(ValueError, 'changed'):
                identity.identify_mapped_gdk(SimpleNamespace(base=0x100000, read=read))


class MeasuredNativeProfiles(unittest.TestCase):
    def test_unknown_profile_and_unported_addresses_fail_closed(self):
        for game, address in ((SimpleNamespace(build_profile='future'), 0xBF35A70),
                              (SimpleNamespace(build_profile=native_layout.GDK), 123)):
            with self.assertRaises(RuntimeError):
                native_layout.rva(game, address)

    def test_guard_reference_changes_require_explicit_remeasurement(self):
        game = SimpleNamespace(build_profile=native_layout.GDK, base=0x100000,
                               read=lambda *_: self.fail('changed guard must fail before read'))
        with self.assertRaisesRegex(RuntimeError, 'reference changed'):
            native_layout.matches(game, 0x3B39C31, b'new reference')

    def test_every_guard_address_is_in_the_fingerprinted_code_range(self):
        from gdk_layout import ADDRESSES, GUARDS
        for reference, (_, guard) in GUARDS.items():
            address = ADDRESSES[reference]
            self.assertTrue(identity.RANGES[0][0] <= address < identity.RANGES[0][0]+identity.RANGES[0][1])
            self.assertLessEqual(address+len(guard), identity.RANGES[0][0]+identity.RANGES[0][1])


if __name__ == '__main__':
    unittest.main()
