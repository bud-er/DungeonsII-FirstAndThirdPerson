"""Certificate-only equivalence must not relax any mapped-byte/layout check."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import executable_identity as identity


PE = 0x80
OPTIONAL = PE + 24
SECTION_TABLE = OPTIONAL + 240
PAYLOAD_SIZE = 0x600


def image(certificate=b'certificate-one!', checksum=0x12345678):
    data = bytearray(PAYLOAD_SIZE)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, PE)
    data[PE:PE + 4] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, PE + 4, 0x8664, 2, 123456, 0, 0, 240, 0x22)
    struct.pack_into('<H', data, OPTIONAL, 0x20B)
    struct.pack_into('<I', data, OPTIONAL + 16, 0x1000)
    struct.pack_into('<Q', data, OPTIONAL + 24, 0x140000000)
    struct.pack_into('<II', data, OPTIONAL + 32, 0x1000, 0x200)
    struct.pack_into('<II', data, OPTIONAL + 56, 0x3000, 0x200)
    struct.pack_into('<I', data, OPTIONAL + 64, checksum)
    struct.pack_into('<I', data, OPTIONAL + 108, 16)
    for index, (name, flags) in enumerate(((b'.text', 0x60000020), (b'.data', 0xC0000040))):
        start = SECTION_TABLE + index * 40
        data[start:start + len(name)] = name
        struct.pack_into('<IIII', data, start + 8, 0x180, 0x1000 * (index + 1), 0x200, 0x200 * (index + 1))
        struct.pack_into('<I', data, start + 36, flags)
        data[0x200 * (index + 1):0x200 * (index + 2)] = bytes([0x41 + index]) * 0x200
    if certificate is not None:
        length = len(certificate) + 8
        cert = struct.pack('<IHH', length, 0x200, 2) + certificate
        cert += bytes((-length) % 8)
        struct.pack_into('<II', data, OPTIONAL + 144, len(data), len(cert))
        data += cert
    return data


def canonical(data):
    payload = bytearray(data[:PAYLOAD_SIZE])
    payload[OPTIONAL + 64:OPTIONAL + 68] = bytes(4)
    payload[OPTIONAL + 144:OPTIONAL + 152] = bytes(8)
    return hashlib.sha256(payload).hexdigest()


class ExecutableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'Dungeons-WinGDK-Shipping.exe'
        self.reference = image()
        self.expectations = {
            'expected_sha256': hashlib.sha256(self.reference).hexdigest(),
            'expected_canonical_sha256': canonical(self.reference),
            'expected_payload_size': PAYLOAD_SIZE,
        }

    def identify(self, data, **changes):
        self.path.write_bytes(data)
        return identity.identify_executable(self.path, **(self.expectations | changes))

    def assert_blocked(self, data):
        result = self.identify(data)
        self.assertFalse(result['compatible'])
        self.assertIsNone(result['match_kind'])
        return result

    def test_original_full_hash_is_first_match(self):
        result = self.identify(self.reference)
        self.assertTrue(result['compatible'])
        self.assertEqual(result['match_kind'], 'exact')
        self.assertIsNone(result['canonical_sha256'])
        self.assertEqual(result['file_identity']['size'], len(self.reference))
        self.assertNotIn(str(self.path), str(result))

    def test_new_certificate_and_checksum_match_without_changing_mapped_bytes(self):
        changed = image(b'new certificate data with a different length', checksum=0xFEDCBA98)
        result = self.identify(changed)
        self.assertTrue(result['compatible'])
        self.assertEqual(result['match_kind'], 'certificate_equivalent')
        self.assertEqual(result['canonical_sha256'], self.expectations['expected_canonical_sha256'])
        self.assertEqual(result['payload_size'], PAYLOAD_SIZE)
        self.assertNotEqual(result['sha256'], self.expectations['expected_sha256'])
        self.assertEqual(self.path.read_bytes(), changed)

    def test_removed_certificate_matches(self):
        result = self.identify(image(None, checksum=0))
        self.assertEqual(result['match_kind'], 'certificate_equivalent')

    def test_checksum_only_change_matches(self):
        self.assertEqual(self.identify(image(checksum=7))['match_kind'], 'certificate_equivalent')

    def test_custom_exact_hash_disables_default_canonical_fallback(self):
        self.path.write_bytes(image(b'other-certificate'))
        with mock.patch.object(identity, 'REFERENCE_CANONICAL_SHA256', canonical(self.reference)), \
                mock.patch.object(identity, 'REFERENCE_PAYLOAD_SIZE', PAYLOAD_SIZE):
            result = identity.identify_executable(self.path, expected_sha256=self.expectations['expected_sha256'])
        self.assertFalse(result['compatible'])
        self.assertIsNone(result['canonical_sha256'])

    def test_exact_non_pe_fixture_does_not_require_pe_parser(self):
        data = b'pinned test fixture'
        self.path.write_bytes(data)
        result = identity.identify_executable(self.path, expected_sha256=hashlib.sha256(data).hexdigest())
        self.assertEqual(result['match_kind'], 'exact')

    def test_default_pinned_fallback_selects_reference_expectations(self):
        self.path.write_bytes(image(b'other-certificate'))
        with mock.patch.object(identity, 'REFERENCE_FULL_SHA256', self.expectations['expected_sha256']), \
                mock.patch.object(identity, 'REFERENCE_CANONICAL_SHA256', canonical(self.reference)), \
                mock.patch.object(identity, 'REFERENCE_PAYLOAD_SIZE', PAYLOAD_SIZE):
            result = identity.identify_executable(self.path, expected_sha256=self.expectations['expected_sha256'])
        self.assertEqual(result['match_kind'], 'certificate_equivalent')

    def test_every_non_certificate_byte_region_stays_pinned(self):
        # DOS stub, timestamp, entrypoint, image base, imports, header padding,
        # executable code, section alignment padding and initialized data.
        for offset in (0x40, PE + 8, OPTIONAL + 16, OPTIONAL + 24, OPTIONAL + 120,
                       SECTION_TABLE + 80, 0x200, 0x3FF, 0x400, 0x5FF):
            with self.subTest(offset=offset):
                changed = bytearray(self.reference)
                changed[offset] ^= 1
                self.assert_blocked(changed)

    def test_section_headers_and_payload_length_stay_pinned(self):
        changed = bytearray(self.reference)
        changed[SECTION_TABLE] = ord('x')
        self.assert_blocked(changed)
        result = self.identify(image(b'different'), expected_payload_size=PAYLOAD_SIZE + 1)
        self.assertFalse(result['compatible'])

    def test_certificate_may_not_hide_code_or_a_file_overlay(self):
        for cert_offset, cert_size in ((0x200, len(self.reference) - 0x200),
                                      (PAYLOAD_SIZE + 8, len(self.reference) - PAYLOAD_SIZE - 8),
                                      (PAYLOAD_SIZE, 0), (0, 24),
                                      (PAYLOAD_SIZE, len(self.reference) - PAYLOAD_SIZE - 8)):
            with self.subTest(offset=cert_offset, size=cert_size):
                changed = bytearray(self.reference)
                struct.pack_into('<II', changed, OPTIONAL + 144, cert_offset, cert_size)
                self.assertIn('identity_error', self.assert_blocked(changed))
        self.assertIn('identity_error', self.assert_blocked(self.reference + b'overlay!'))
        self.assertIn('identity_error', self.assert_blocked(image(None) + b'overlay!'))

    def test_malformed_certificate_entries_are_rejected(self):
        for length, revision, kind in ((0, 0x200, 2), (8, 0x200, 2),
                                       (100000, 0x200, 2), (24, 0x100, 2), (24, 0x200, 1)):
            with self.subTest(length=length, revision=revision, kind=kind):
                changed = bytearray(self.reference)
                struct.pack_into('<IHH', changed, PAYLOAD_SIZE, length, revision, kind)
                self.assertIn('identity_error', self.assert_blocked(changed))
        changed = image(b'x')
        changed[-1] = 1
        self.assertIn('identity_error', self.assert_blocked(changed))

    def test_certificate_table_can_contain_multiple_structurally_valid_entries(self):
        changed = image(b'new')
        cert = struct.pack('<IHH', 16, 0x200, 2) + b'12345678'
        changed += cert
        struct.pack_into('<I', changed, OPTIONAL + 148, len(changed) - PAYLOAD_SIZE)
        self.assertEqual(self.identify(changed)['match_kind'], 'certificate_equivalent')

    def test_invalid_pe_shape_is_rejected_even_if_canonical_expectation_is_supplied(self):
        mutations = (
            (0, '<H', 0), (0x3C, '<I', 0xFFFFFFFF), (PE, '<I', 0),
            (PE + 4, '<H', 0x14C), (PE + 6, '<H', 97), (PE + 20, '<H', 224),
            (OPTIONAL, '<H', 0x10B), (OPTIONAL + 108, '<I', 15),
            (OPTIONAL + 32, '<I', 0x1100), (OPTIONAL + 36, '<I', 0x180),
            (OPTIONAL + 56, '<I', 0x2000), (OPTIONAL + 60, '<I', 0x100),
            (SECTION_TABLE + 40 + 12, '<I', 0x1000),
            (SECTION_TABLE + 40 + 20, '<I', 0x200),
            (SECTION_TABLE + 20, '<I', 0x201),
            (SECTION_TABLE + 16, '<I', 0x201),
        )
        for offset, fmt, value in mutations:
            with self.subTest(offset=offset, value=value):
                changed = image(b'other')
                struct.pack_into(fmt, changed, offset, value)
                result = self.identify(changed, expected_canonical_sha256=canonical(changed))
                self.assertFalse(result['compatible'])
                self.assertIn('identity_error', result)

    def test_truncated_headers_sections_and_certificate_are_rejected(self):
        for length in (12, PE + 10, OPTIONAL + 230, 0x300, PAYLOAD_SIZE - 1, len(self.reference) - 1):
            with self.subTest(length=length):
                self.assertIn('identity_error', self.assert_blocked(self.reference[:length]))

    def test_file_size_bound_applies_before_reading(self):
        self.path.write_bytes(self.reference)
        with mock.patch.object(identity, 'MAX_EXECUTABLE_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'bounded regular file'):
                identity.identify_executable(self.path)

    def test_file_change_during_hash_raises_instead_of_returning_match(self):
        self.path.write_bytes(self.reference)
        original = identity._hash_stream

        def racing_hash(stream, length, fields=()):
            digest = original(stream, length, fields)
            with self.path.open('ab') as target:
                target.write(b'x')
            return digest

        with mock.patch.object(identity, '_hash_stream', side_effect=racing_hash):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                identity.identify_executable(self.path, **self.expectations)

    def test_path_replacement_after_open_is_detected(self):
        self.path.write_bytes(self.reference)
        original = identity._hash_stream
        replacement = self.path.with_suffix('.replacement')
        replacement.write_bytes(self.reference)

        def replace_after_hash(stream, length, fields=()):
            digest = original(stream, length, fields)
            # Windows may forbid replacing an open file. A changed path stat
            # is the same proof of mismatch and portable across test hosts.
            return digest

        before = self.path.stat()
        after = replacement.stat()
        with mock.patch.object(identity, '_hash_stream', side_effect=replace_after_hash), \
                mock.patch.object(Path, 'stat', side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                identity.identify_executable(self.path, **self.expectations)

    def test_invalid_expected_hashes_and_payload_configuration_fail(self):
        self.path.write_bytes(self.reference)
        for options in ({'expected_sha256': 'not a hash'},
                        {'expected_payload_size': 2},
                        {'expected_canonical_sha256': 'bad', 'expected_payload_size': 2},
                        {'expected_canonical_sha256': canonical(self.reference)},
                        {'expected_canonical_sha256': canonical(self.reference), 'expected_payload_size': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                identity.identify_executable(self.path, **options)


if __name__ == '__main__':
    unittest.main()
