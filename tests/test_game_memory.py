from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import game_memory


class MemoryFailureDetails(unittest.TestCase):
    def test_partial_read_preserves_os_error_with_address_and_byte_count(self):
        reader=game_memory.GameReader.__new__(game_memory.GameReader)
        reader.handle=123
        reader.kernel=Mock()
        def partial(handle,address,buffer,length,count):
            count._obj.value=3
            return False
        reader.kernel.ReadProcessMemory.side_effect=partial
        with patch.object(game_memory.C,'get_last_error',return_value=299):
            with self.assertRaises(OSError) as raised:
                reader.read(0x123450,8)
        self.assertEqual(raised.exception.winerror,299)
        self.assertIn('read at 0x123450, 3/8 bytes',str(raised.exception))

    def test_partial_write_preserves_error_and_failed_request(self):
        from external_camera import CameraMemory
        writer=CameraMemory.__new__(CameraMemory)
        writer.handle=123
        writer.kernel=Mock()
        writer.kernel.WriteProcessMemory.return_value=False
        with patch.object(game_memory.C,'get_last_error',return_value=5):
            with self.assertRaises(OSError) as raised:
                writer.write(0x654320,b'ab')
        self.assertEqual(raised.exception.winerror,5)
        self.assertIn('write at 0x654320, 0/2 bytes',str(raised.exception))


class ProcessDiscovery(unittest.TestCase):
    def scan(self, codes, *, names=None, open_fail=(), open_error=5, query_fail=(), exit_after=(), unrelated=False):
        kernel = Mock()
        kernel.OpenProcess.side_effect = lambda rights, inherit, pid: None if pid in open_fail else pid
        checks = {}
        def status(handle, destination):
            if codes[handle] is None:
                return False
            checks[handle] = checks.get(handle, 0) + 1
            destination._obj.value = 0 if handle in exit_after and checks[handle] > 1 else codes[handle]
            return True
        kernel.GetExitCodeProcess.side_effect = status
        names = names or {pid: game_memory.GAME_EXE for pid in codes}
        def image(handle, flags, destination, size):
            if handle in query_fail:
                return False
            destination.value = str(Path('C:/Games') / names[handle])
            return True
        kernel.QueryFullProcessImageNameW.side_effect = image
        def times(handle, created, *_):
            created._obj.dwHighDateTime = 2
            created._obj.dwLowDateTime = handle
            return True
        kernel.GetProcessTimes.side_effect = times
        rows = '\n'.join(f'"{names[pid]}","{pid}"' for pid in codes)
        if unrelated:
            rows += '\n"Dungeons.exe","999"\n"SomeOtherGame.exe","998"'
        with patch.object(game_memory.subprocess, 'run', return_value=Mock(stdout=rows)) as tasklist, \
                patch.object(game_memory.C, 'WinDLL', return_value=kernel), \
                patch.object(game_memory.C, 'get_last_error', return_value=open_error):
            result = game_memory.discover_game_processes()
        self.assertEqual(kernel.CloseHandle.call_count, len(codes) - len(open_fail))
        self.assertTrue(all(call.args[0] == game_memory.QUERY_LIMITED_INFORMATION
                            for call in kernel.OpenProcess.call_args_list))
        self.assertEqual(tasklist.call_args.kwargs['timeout'], 10)
        return result, kernel

    def selected(self, codes, **kwargs):
        candidates, _ = self.scan(codes, **kwargs)
        with patch.object(game_memory, 'discover_game_processes', return_value=candidates):
            return game_memory.find_game_pid()

    def test_dead_process_with_open_handles_does_not_block_new_process(self):
        self.assertEqual(self.selected({11: 0, 22: 259}), 22)

    def test_only_dead_processes_return_none(self):
        self.assertIsNone(self.selected({11: 0}))

    def test_unreadable_candidate_is_reported_instead_of_silent_wait(self):
        with self.assertRaisesRegex(RuntimeError, 'could not be inspected'):
            self.selected({11: None})

    def test_two_live_processes_are_rejected(self):
        with self.assertRaises(RuntimeError):
            self.selected({11: 259, 22: 259})

    def test_gdk_name_is_detected_without_claiming_hash_compatibility(self):
        names = {11: 'Dungeons-WinGDK-Shipping.exe'}
        candidates, kernel = self.scan({11: 259}, names=names)
        self.assertEqual(candidates, [dict(pid=11, name=names[11],
                         executable=str((Path('C:/Games') / names[11]).resolve()),
                         creation_time=(2 << 32) | 11, error=None)])
        kernel.ReadProcessMemory.assert_not_called()

    def test_discovery_is_case_insensitive_and_excludes_launcher_stub(self):
        self.assertEqual(self.selected({11: 259}, names={11: game_memory.GAME_EXE.upper()},
                                       unrelated=True), 11)

    def test_mixed_store_processes_are_ambiguous(self):
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            self.selected({11: 259, 22: 259}, names={11: game_memory.GAME_EXE,
                          22: 'Dungeons-WinGDK-Shipping.exe'})

    def test_access_denied_candidate_prevents_selecting_other_copy(self):
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            self.selected({11: 259, 22: 259}, open_fail=(22,))

    def test_pid_vanishing_after_tasklist_keeps_waiting_for_game(self):
        self.assertIsNone(self.selected({11: 259}, open_fail=(11,), open_error=87))

    def test_pid_vanishing_after_tasklist_does_not_block_replacement(self):
        self.assertEqual(self.selected({11: 259, 22: 259}, open_fail=(11,), open_error=87), 22)

    def test_access_denied_process_is_still_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'Windows error 5'):
            self.selected({11: 259}, open_fail=(11,), open_error=5)

    def test_image_query_failure_is_reported_with_no_partial_identity(self):
        candidates, _ = self.scan({11: 259}, query_fail=(11,))
        self.assertIsNotNone(candidates[0]['error'])
        self.assertIsNone(candidates[0]['executable'])
        self.assertIsNone(candidates[0]['creation_time'])

    def test_process_exiting_during_discovery_is_ignored(self):
        self.assertIsNone(self.selected({11: 259}, exit_after=(11,)))

    def test_failed_tasklist_is_not_mistaken_for_no_game(self):
        with patch.object(game_memory.subprocess, 'run', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(OSError, 'unavailable'):
                game_memory.discover_game_processes()

    def test_changed_process_name_is_reported(self):
        with patch.object(game_memory, '_process_identity', return_value=(Path('C:/other.exe'), 1)):
            candidates, _ = self.scan({11: 259})
        self.assertIn('changed', candidates[0]['error'])


class ValidatedProcessAccess(unittest.TestCase):
    def setUp(self):
        self.kernel = Mock()
        self.events = []
        def open_process(access, inherit, pid):
            self.events.append(('open', access))
            return 101 if access == game_memory.QUERY_LIMITED_INFORMATION else 202
        self.kernel.OpenProcess.side_effect = open_process
        def status(handle, destination):
            destination._obj.value = game_memory.STILL_ACTIVE
            return True
        self.kernel.GetExitCodeProcess.side_effect = status
        self.psapi = Mock()
        def modules(handle, destination, size, needed, flags):
            destination[0] = 0x140000000
            needed._obj.value = game_memory.C.sizeof(game_memory.W.HMODULE)
            return True
        self.psapi.EnumProcessModulesEx.side_effect = modules
        self.path = Path('C:/Games') / game_memory.GAME_EXE

    def open_reader(self, identities=None, validation_error=None):
        class Writer(game_memory.GameReader):
            ACCESS = 0x0438
        def validate(exe, game):
            self.events.append(('validate', exe))
            if validation_error:
                raise validation_error
            return exe
        identities = identities or [(self.path, 77), (self.path, 77)]
        with patch.object(game_memory, '_query_kernel', return_value=self.kernel), \
                patch.object(game_memory, '_process_identity', side_effect=identities), \
                patch.object(game_memory, 'validate_executable', side_effect=validate), \
                patch.object(game_memory.C, 'WinDLL', return_value=self.psapi), \
                patch.object(Writer, 'read', return_value=b'MZ'):
            return Writer(pid=123)

    def test_unsupported_executable_never_requests_vm_access(self):
        with self.assertRaisesRegex(RuntimeError, 'unsupported'):
            self.open_reader(validation_error=RuntimeError('unsupported'))
        self.assertEqual([event for event in self.events if event[0] == 'open'],
                         [('open', game_memory.QUERY_LIMITED_INFORMATION)])
        self.kernel.CloseHandle.assert_called_once_with(101)
        self.psapi.EnumProcessModulesEx.assert_not_called()

    def test_supported_image_is_hashed_before_requesting_write_access(self):
        reader = self.open_reader()
        self.assertEqual(self.events, [('open', game_memory.QUERY_LIMITED_INFORMATION),
                                      ('validate', self.path), ('open', 0x0438)])
        self.assertEqual(reader.handle, 202)
        self.assertEqual(reader.executable, self.path)
        self.kernel.CloseHandle.assert_called_once_with(101)
        reader.close()
        self.assertEqual(self.kernel.CloseHandle.call_args.args, (202,))

    def test_changed_creation_time_or_path_rejects_and_closes_both_handles(self):
        for identity in ((self.path, 78), (Path('C:/changed.exe'), 77)):
            with self.subTest(identity=identity):
                self.kernel.CloseHandle.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'changed during validation'):
                    self.open_reader(identities=[(self.path, 77), identity])
                self.assertEqual({call.args[0] for call in self.kernel.CloseHandle.call_args_list}, {101, 202})
                self.psapi.EnumProcessModulesEx.assert_not_called()

    def test_write_access_failure_closes_initial_query_handle(self):
        self.kernel.OpenProcess.side_effect = [101, None]
        with self.assertRaises(OSError):
            self.open_reader()
        self.kernel.CloseHandle.assert_called_once_with(101)


class ExecutableValidation(unittest.TestCase):
    def test_matching_build_works_in_an_arbitrary_install_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/game_memory.GAME_EXE
            exe.write_bytes(b'supported game fixture')
            fingerprint=game_memory.hashlib.sha256(exe.read_bytes()).hexdigest()
            with patch.object(game_memory,'GAME_SHA256',fingerprint):
                self.assertEqual(game_memory.validate_executable(exe),exe.resolve())

    def test_updated_build_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/game_memory.GAME_EXE
            exe.write_bytes(b'changed game')
            with self.assertRaisesRegex(RuntimeError,'Unsupported game version'):
                game_memory.validate_executable(exe)

    def test_gdk_executable_still_requires_the_exact_same_validated_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / 'Dungeons-WinGDK-Shipping.exe'
            exe.write_bytes(b'unvalidated GDK edition')
            with self.assertRaisesRegex(RuntimeError, 'Unsupported game version'):
                game_memory.validate_executable(exe)
            fingerprint = game_memory.hashlib.sha256(exe.read_bytes()).hexdigest()
            with patch.object(game_memory, 'GAME_SHA256', fingerprint):
                self.assertEqual(game_memory.validate_executable(exe), exe.resolve())

    def test_explicit_gdk_install_selection_accepts_only_matching_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / game_memory.GAME_EXE_PATHS[1]
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'supported game fixture')
            fingerprint = game_memory.hashlib.sha256(exe.read_bytes()).hexdigest()
            with patch.object(game_memory, 'GAME_SHA256', fingerprint):
                self.assertEqual(game_memory.validate_executable(exe, game=directory), exe.resolve())
                with self.assertRaisesRegex(RuntimeError, 'selected installation'):
                    game_memory.validate_executable(exe, game=exe.parent)

    def test_wrong_process_name_is_refused_before_hashing(self):
        with patch.object(game_memory.hashlib,'file_digest') as digest:
            with self.assertRaisesRegex(RuntimeError,'Refusing'):
                game_memory.validate_executable('unrelated.exe')
        digest.assert_not_called()

    def test_explicit_install_selection_remains_enforced(self):
        with self.assertRaisesRegex(RuntimeError,'selected installation'):
            game_memory.validate_executable(game_memory.GAME_EXE, game=Path('another-install'))


if __name__ == '__main__':
    unittest.main()
