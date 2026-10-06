import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import game_compatibility as compatibility


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def manifest_text(**changes):
    values = dict(appid='1912410', StateFlags='4', installdir='Minecraft Dungeons II',
                  buildid='25647713', TargetBuildID='25647713', UpdateResult='0',
                  BytesToDownload='12', BytesDownloaded='12', BytesToStage='40', BytesStaged='40')
    values.update(changes)
    return '"AppState"\n{\n' + '\n'.join(quoted(k)+' '+quoted(v) for k, v in values.items()) + '\n}\n'


class VdfTests(unittest.TestCase):
    def test_comments_escaped_paths_bare_values_and_bom(self):
        parsed = compatibility.parse_vdf('\ufeff// comment\n"LibraryFolders" { 0 { "path" "C:\\\\Steam Games" } }')
        self.assertEqual(parsed, {'libraryfolders': {'0': {'path': 'C:\\Steam Games'}}})
        self.assertEqual(compatibility.parse_vdf('key "// literal" // ignored'), {'key': '// literal'})

    def test_torn_duplicate_or_over_nested_metadata_is_rejected(self):
        malformed = ('"AppState" { "appid" "1912410"', '"x" "unfinished',
                     '"key" "one" "KEY" "two"', '}', 'key', 'key }',
                     'key { ' * 18 + '} ' * 18)
        for text in malformed:
            with self.subTest(text=text), self.assertRaises(compatibility.CompatibilityUnknown):
                compatibility.parse_vdf(text)

    def test_size_limit_is_enforced(self):
        with self.assertRaises(compatibility.CompatibilityUnknown):
            compatibility.parse_vdf(' ' * (compatibility.MAX_VDF_BYTES + 1))


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        discovery = patch.object(compatibility, 'running_game_processes', return_value=[])
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Steam & 日本語'
        self.original = b'known executable fixture'
        self.expected = hashlib.sha256(self.original).hexdigest()
        self.manifest, self.executable = self.install(self.root)
        self.checker = compatibility.GameCompatibilityChecker([self.root], expected_sha256=self.expected)

    def install(self, root, content=None, **changes):
        manifest = root / 'steamapps/appmanifest_1912410.acf'
        executable = root / 'steamapps/common/Minecraft Dungeons II' / compatibility.EXE_RELATIVE
        executable.parent.mkdir(parents=True)
        executable.write_bytes(self.original if content is None else content)
        manifest.write_text(manifest_text(**changes), encoding='utf-8')
        return manifest, executable

    def test_stable_copy_matches_then_reuses_advisory_cache(self):
        first = self.checker.check()
        second = self.checker.check()
        self.assertEqual(first['state'], 'compatible')
        self.assertFalse(first['copies'][0]['hash_cached'])
        self.assertTrue(second['copies'][0]['hash_cached'])
        self.assertEqual(first['copies'][0]['sha256'], self.expected)
        self.assertEqual(first['copies'][0]['steam_build'], '25647713')
        json.dumps(second)

    def test_changed_executable_is_unsupported(self):
        self.executable.write_bytes(b'new game executable')
        result = self.checker.check()
        self.assertEqual(result['state'], 'unsupported')
        self.assertNotEqual(result['copies'][0]['sha256'], self.expected)

    def test_same_size_replacement_with_preserved_mtime_does_not_reuse_hash(self):
        self.checker.check()
        before = self.executable.stat()
        replacement = self.executable.with_suffix('.replacement')
        replacement.write_bytes(b'x' * len(self.original))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.executable)
        result = self.checker.check()
        self.assertEqual(result['state'], 'unsupported')
        self.assertFalse(result['copies'][0]['hash_cached'])

    def test_manifest_update_invalidates_cache_even_with_same_executable(self):
        self.checker.check()
        self.manifest.write_text(manifest_text(buildid='25647714', TargetBuildID='25647714'), encoding='utf-8')
        result = self.checker.check()
        self.assertEqual(result['state'], 'compatible')
        self.assertFalse(result['copies'][0]['hash_cached'])
        self.assertEqual(result['copies'][0]['steam_build'], '25647714')

    def test_updating_or_incomplete_manifest_never_hashes_game(self):
        changes = ({'StateFlags': '6'}, {'StateFlags': '1026'}, {'StateFlags': 'invalid'},
                   {'TargetBuildID': '25647714'}, {'BytesDownloaded': '3'}, {'BytesStaged': '2'},
                   {'UpdateResult': '1'}, {'ScheduledAutoUpdate': '123'}, {'appid': '999'})
        for change in changes:
            self.manifest.write_text(manifest_text(**change), encoding='utf-8')
            with self.subTest(change=change), patch.object(self.checker, '_hash') as hash_game:
                self.assertEqual(self.checker.check()['state'], 'unknown')
                hash_game.assert_not_called()

    def test_recovery_after_steam_update_rehashes(self):
        self.checker.check()
        self.manifest.write_text(manifest_text(StateFlags='6'), encoding='utf-8')
        self.assertEqual(self.checker.check()['state'], 'unknown')
        self.manifest.write_text(manifest_text(), encoding='utf-8')
        self.assertFalse(self.checker.check()['copies'][0]['hash_cached'])

    def test_traversal_and_absolute_install_folder_are_not_read(self):
        for name in ('..', '../outside', 'folder/../../outside', r'C:\\outside', 'safe:stream', 'bad. ', ''):
            self.manifest.write_text(manifest_text(installdir=name), encoding='utf-8')
            with self.subTest(name=name), patch.object(self.checker, '_hash') as hash_game:
                self.assertEqual(self.checker.check()['state'], 'unknown')
                hash_game.assert_not_called()

    def test_file_or_manifest_disappearing_is_not_compatible(self):
        self.executable.unlink()
        self.assertEqual(self.checker.check()['state'], 'unknown')
        self.manifest.unlink()
        self.assertEqual(self.checker.check()['state'], 'not_found')

    def test_manifest_torn_or_unreadable_is_unknown(self):
        self.manifest.write_text('"AppState" {', encoding='utf-8')
        self.assertEqual(self.checker.check()['state'], 'unknown')
        with patch.object(compatibility, 'read_vdf', side_effect=PermissionError('locked')):
            self.assertEqual(self.checker.check()['state'], 'unknown')

    def test_manifest_changed_during_hash_invalidates_result(self):
        original = self.checker._hash
        def changed(*args):
            result = original(*args)
            self.manifest.write_text(manifest_text(StateFlags='6'), encoding='utf-8')
            return result
        with patch.object(self.checker, '_hash', side_effect=changed):
            self.assertEqual(self.checker.check()['state'], 'unknown')
        self.assertEqual(self.checker._hashes, {})

    def test_executable_changed_after_cache_lookup_invalidates_result(self):
        self.checker.check()
        original = self.checker._hash
        def changed(*args):
            result = original(*args)
            self.executable.write_bytes(b'changed after cache')
            return result
        with patch.object(self.checker, '_hash', side_effect=changed):
            self.assertEqual(self.checker.check()['state'], 'unknown')

    def test_multiple_installed_copies_are_all_reported(self):
        other = self.base / 'Other Library'
        self.install(other, b'unsupported copy')
        checker = compatibility.GameCompatibilityChecker([self.root, other], expected_sha256=self.expected)
        result = checker.check()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual({item['state'] for item in result['copies']}, {'compatible', 'unsupported'})
        self.assertEqual(len(result['copies']), 2)

    def test_modern_and_legacy_library_lists_find_external_copy(self):
        central = self.base / 'Central Steam'
        (central / 'steamapps').mkdir(parents=True)
        library_vdf = central / 'steamapps/libraryfolders.vdf'
        for entry in (quoted(self.root), '{ "path" '+quoted(self.root)+' "apps" { "1912410" "123" } }'):
            library_vdf.write_text('"libraryfolders" { "1" '+entry+' }', encoding='utf-8')
            checker = compatibility.GameCompatibilityChecker([central], expected_sha256=self.expected)
            with self.subTest(entry=entry):
                self.assertEqual(checker.check()['state'], 'compatible')

    def test_library_claiming_installed_game_without_manifest_is_unknown(self):
        central = self.base / 'Central Steam'
        (central / 'steamapps').mkdir(parents=True)
        (central / 'steamapps/libraryfolders.vdf').write_text(
            '"libraryfolders" { "1" { "path" '+quoted(self.base / 'offline')+
            ' "apps" { "1912410" "123" } } }', encoding='utf-8')
        checker = compatibility.GameCompatibilityChecker([central], expected_sha256=self.expected)
        result = checker.check()
        self.assertEqual(result['state'], 'unknown')
        self.assertTrue(result['discovery_problems'])

    def test_empty_roots_are_not_found_and_discovery_errors_are_unknown(self):
        result = compatibility.GameCompatibilityChecker([]).check()
        self.assertEqual(result['state'], 'not_found')
        self.assertIn('Launch the game', result['reason'])
        self.assertEqual(result['source'], 'none')
        with patch.object(compatibility, 'registry_steam_roots', return_value=([], ['access denied'])):
            self.assertEqual(compatibility.GameCompatibilityChecker().check()['state'], 'unknown')

    def running(self, executable=None, **changes):
        executable = executable or self.executable
        result = dict(pid=1234, name=executable.name, executable=str(executable), creation_time=1000000, error=None)
        result.update(changes)
        return result

    def standalone(self, name=None, content=None):
        executable = self.base / 'Launcher independent & 日本語' / (name or compatibility.GAME_EXE)
        executable.parent.mkdir(exist_ok=True)
        executable.write_bytes(self.original if content is None else content)
        return executable

    def test_selected_executable_needs_no_steam_and_reuses_hash(self):
        executable = self.standalone()
        checker = compatibility.GameCompatibilityChecker([], expected_sha256=self.expected)
        first = checker.check(selected_executable=executable)
        second = checker.check(selected_executable=str(executable))
        self.assertEqual((first['state'], first['source']), ('compatible', 'selected'))
        self.assertFalse(first['copies'][0]['hash_cached'])
        self.assertTrue(second['copies'][0]['hash_cached'])
        self.assertNotIn('steam_build', first['copies'][0])

    def test_selected_unknown_gdk_build_is_unsupported(self):
        executable = self.standalone('Dungeons-WinGDK-Shipping.exe', b'untested store build')
        result = self.checker.check(selected_executable=executable)
        self.assertEqual((result['state'], result['source']), ('unsupported', 'selected'))
        self.assertEqual(result['copies'][0]['executable'], str(executable))

    def test_matching_hash_is_accepted_regardless_of_recognized_build_name(self):
        executable = self.standalone('Dungeons-WinGDK-Shipping.exe')
        result = self.checker.check(selected_executable=executable)
        self.assertEqual(result['state'], 'compatible')
        self.assertEqual(result['copies'][0]['sha256'], self.expected)

    def test_selected_copy_takes_precedence_over_incompatible_steam(self):
        executable = self.standalone()
        self.executable.write_bytes(b'other unsupported installation')
        with patch.object(self.checker, '_libraries', side_effect=AssertionError('must not inspect Steam')):
            self.assertEqual(self.checker.check(selected_executable=executable)['state'], 'compatible')

    def test_selected_relative_launcher_or_absent_path_is_unknown(self):
        wrong = self.standalone('MinecraftLauncher.exe')
        missing = self.base / 'missing' / compatibility.GAME_EXE
        for path in (Path(compatibility.GAME_EXE), wrong, missing, ''):
            with self.subTest(path=path):
                result = self.checker.check(selected_executable=path)
                self.assertEqual((result['state'], result['source']), ('unknown', 'selected'))

    def test_selected_changed_after_hash_is_unknown_and_drops_cache(self):
        executable = self.standalone()
        self.checker.check(selected_executable=executable)
        original = self.checker._hash
        def changed(*args):
            result = original(*args)
            executable.write_bytes(b'changed during read')
            return result
        with patch.object(self.checker, '_hash', side_effect=changed):
            result = self.checker.check(selected_executable=executable)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.checker._hashes, {})

    def test_running_executable_precedes_selected_and_installed_copies(self):
        executable = self.standalone()
        candidate = self.running(executable)
        self.discovery.return_value = [candidate]
        self.executable.write_bytes(b'not the running executable')
        with patch.object(self.checker, '_libraries', side_effect=AssertionError('must not inspect Steam')):
            result = self.checker.check(selected_executable=self.executable)
        self.assertEqual((result['state'], result['source']), ('compatible', 'running'))
        self.assertEqual(result['copies'][0]['executable'], str(executable))
        self.assertEqual(self.discovery.call_count, 2)

    def test_running_unknown_build_never_falls_back_to_compatible_steam(self):
        executable = self.standalone('Dungeons-WinGDK-Shipping.exe', b'unknown build')
        self.discovery.return_value = [self.running(executable)]
        result = self.checker.check()
        self.assertEqual((result['state'], result['source']), ('unsupported', 'running'))
        self.assertNotEqual(result['copies'][0]['sha256'], self.expected)

    def test_duplicate_or_inaccessible_running_game_cannot_fall_back(self):
        cases = ([self.running(), self.running(pid=9876)],
                 [self.running(executable=None, creation_time=None, error='access denied')],
                 [self.running(creation_time=None)], [self.running(name='unrecognized.exe')])
        for candidates in cases:
            self.discovery.return_value = candidates
            with self.subTest(candidates=candidates), patch.object(self.checker, '_hash') as hash_game:
                result = self.checker.check(selected_executable=self.executable)
                self.assertEqual((result['state'], result['source']), ('unknown', 'running'))
                hash_game.assert_not_called()

    def test_process_exit_reuse_or_new_duplicate_during_hash_is_unknown(self):
        original = self.running()
        changed = ([], [self.running(creation_time=1000001)],
                   [self.running(pid=4321)], [original, self.running(pid=4321)],
                   [self.running(self.standalone())], [self.running(error='query failed')])
        for after in changed:
            self.discovery.side_effect = [[original], after]
            with self.subTest(after=after):
                result = self.checker.check()
                self.assertEqual(result['state'], 'unknown')
                self.assertNotIn('sha256', result['copies'][0])
                self.assertEqual(self.checker._hashes, {})

    def test_file_replaced_during_process_recheck_is_unknown(self):
        original = self.running()
        def changed():
            self.executable.write_bytes(b'replaced during second process scan')
            return [original]
        calls = iter((lambda: [original], changed))
        self.discovery.side_effect = lambda: next(calls)()
        result = self.checker.check()
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('sha256', result['copies'][0])
        self.assertEqual(self.checker._hashes, {})

    def test_discovery_timeout_failure_or_invalid_result_is_unknown(self):
        errors = (subprocess.TimeoutExpired('tasklist', 10), subprocess.CalledProcessError(1, 'tasklist'),
                  PermissionError('denied'), RuntimeError('invalid pid'))
        for error in errors:
            self.discovery.side_effect = error
            with self.subTest(error=error):
                self.assertEqual(self.checker.check()['state'], 'unknown')
        self.discovery.side_effect = None
        for value in ({}, [None], [{'executable': ['invalid path']}],
                      [{}] * (compatibility.MAX_GAME_PROCESSES + 1)):
            self.discovery.return_value = value
            with self.subTest(value=value):
                self.assertEqual(self.checker.check()['state'], 'unknown')

    def test_process_discovery_dependency_can_be_injected(self):
        checker = compatibility.GameCompatibilityChecker([], expected_sha256=self.expected,
                                                         process_discovery=lambda: [self.running()])
        result = checker.check()
        self.assertEqual((result['state'], result['source']), ('compatible', 'running'))
        self.discovery.assert_not_called()

    def test_shareable_report_contains_only_build_facts(self):
        executable = self.standalone('Dungeons-WinGDK-Shipping.exe', b'unknown store executable')
        self.discovery.return_value = [self.running(executable)]
        result = self.checker.check()
        result['reason'] = str(self.base / 'private user path')
        result['discovery_problems'] = [str(self.base / 'private error path')]
        result['copies'][0]['extra_private_detail'] = 'secret-library-name'
        report = compatibility.compatibility_report(result)
        serialized = json.dumps(report)
        self.assertNotIn(str(self.base), serialized)
        self.assertNotIn('private', serialized)
        self.assertNotIn('secret-library-name', serialized)
        self.assertNotIn('creation_time', serialized)
        self.assertNotIn('pid', serialized)
        self.assertEqual(report['copies'][0]['executable_name'], 'Dungeons-WinGDK-Shipping.exe')
        self.assertEqual(report['copies'][0]['build_family'], 'WinGDK')
        self.assertEqual(report['copies'][0]['sha256'], result['copies'][0]['sha256'])
        self.assertEqual(report['copies'][0]['size'], executable.stat().st_size)
        self.assertEqual(report['supported_sha256'], compatibility.GAME_SHA256)

    def test_shareable_report_does_not_preserve_unknown_path_or_error_name(self):
        result = {'state': 'unknown', 'source': 'running', 'checked_utc': 'private datetime',
                  'copies': [{'executable': r'C:\Users\Private\personal.exe',
                              'name': 'personal.exe', 'state': 'unknown', 'reason': 'private error'}]}
        report = compatibility.compatibility_report(result)
        serialized = json.dumps(report)
        self.assertNotIn('Private', serialized)
        self.assertNotIn('personal', serialized)
        self.assertEqual(report['copies'][0]['executable_name'], 'unidentified')
        self.assertIsNone(report['checked_utc'])


if __name__ == '__main__':
    unittest.main()
