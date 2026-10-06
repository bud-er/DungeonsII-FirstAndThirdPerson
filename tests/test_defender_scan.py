import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import defender_scan as scan


def clean_output(directory):
    return f'Scan starting...\r\nScan finished.\r\nScanning {directory} found no threats.\r\n'


def command_result(stdout, returncode=0, stderr=''):
    return dict(returncode=returncode, stdout=stdout, stderr=stderr, timed_out=False)


def healthy_status():
    return dict(AMServiceEnabled=True, AntivirusEnabled=True, RealTimeProtectionEnabled=True,
                AMRunningMode='Normal', AMEngineVersion='1.1.1.1', AMProductVersion='4.18.1.1',
                AntivirusSignatureVersion='1.1.1.1', AntivirusSignatureAge=0,
                AntivirusSignatureLastUpdated='2026-10-03T00:00:00Z', ToolVersion='4.18.1.1',
                ToolSignatureStatus='Valid', ToolSigner='CN=Microsoft Windows Publisher, O=Microsoft Corporation, C=US')


class OutputTests(unittest.TestCase):
    def test_explicit_completed_clean_scan(self):
        directory = Path(r'C:\Release Folder')
        self.assertTrue(scan.parse_scan_output(0, clean_output(directory), '', directory))
        self.assertTrue(scan.parse_scan_output(0, 'Scan starting...\nScan finished.\nNo threats found.', '', directory))

    def test_zero_can_mean_remediated_and_must_not_pass(self):
        for output in ('Scan starting...\nScan finished.\nThreat found and successfully remediated.',
                       'Scan starting...\nScan finished.\nThreats detected: 1',
                       '', 'No threats found.', 'Scan starting...\nScan finished.'):
            with self.subTest(output=output), self.assertRaises(scan.DefenderScanError):
                scan.parse_scan_output(0, output, '', Path('release'))

    def test_errors_cancellation_and_unrecognized_output_fail_closed(self):
        clean = clean_output('release')
        for code, out, err in ((2, clean, ''), (0, clean, 'Error'),
                               (0, clean+'Warning: a file was skipped', ''),
                               (0, clean+'Threat detected', ''),
                               (0, clean.replace('Scan finished.', 'Scan cancelled.'), ''),
                               (0, 'Keine Bedrohungen gefunden.', '')):
            with self.subTest(output=out), self.assertRaises(scan.DefenderScanError):
                scan.parse_scan_output(code, out, err, Path('release'))

    def test_wrong_target_or_clean_words_in_other_output_are_not_proof(self):
        for out in (clean_output('other'), clean_output('release')+'\nScan starting...',
                    'Scan starting...\nScan finished.\nWarning: No threats found.'):
            with self.subTest(output=out), self.assertRaises(scan.DefenderScanError):
                scan.parse_scan_output(0, out, '', Path('release'))


class CommandTests(unittest.TestCase):
    @patch.object(scan.subprocess, 'run')
    def test_command_is_not_a_shell_and_raw_output_is_hashed(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, b'abc\r\n', b'')
        result = scan._run(['MpCmdRun.exe', '-File', 'a & b'], timeout=5)
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertEqual(run.call_args.args[0][-1], 'a & b')
        self.assertEqual(result['stdout_sha256'], hashlib.sha256(b'abc\r\n').hexdigest())
        self.assertEqual(result['returncode'], 0)
        self.assertIn('finished_utc', result)

    @patch.object(scan.subprocess, 'run')
    def test_timeout_retains_partial_output_and_never_succeeds(self, run):
        run.side_effect = subprocess.TimeoutExpired('scanner', 1, output=b'Scan starting...')
        result = scan._run(['scanner'], timeout=1)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['returncode'])
        self.assertEqual(result['stdout'], 'Scan starting...')

    @patch.object(scan.subprocess, 'run', side_effect=PermissionError('blocked'))
    def test_start_failure_is_recorded(self, run):
        result = scan._run(['scanner'], timeout=1)
        self.assertIn('blocked', result['error'])
        self.assertIsNone(result['returncode'])


class StatusTests(unittest.TestCase):
    def test_status_child_uses_only_builtin_windows_powershell_modules(self):
        inherited = {'PSModulePath': r'C:\Users\Test\custom-modules;C:\Program Files\PowerShell\7\Modules',
                     'KEEP_OTHER_ENV': 'unchanged'}
        with patch.dict(scan.os.environ, inherited), patch.object(
                scan, '_run', return_value=command_result(json.dumps(healthy_status()))) as run:
            scan._status(Path('tool'), [])
        command = run.call_args.args[0]
        environment = run.call_args.kwargs['env']
        keys = [key for key in environment if key.casefold() == 'psmodulepath']
        self.assertEqual(len(keys), 1)
        self.assertEqual(environment[keys[0]], str(Path(command[0]).parent / 'Modules'))
        self.assertNotIn('custom-modules', environment[keys[0]])
        self.assertNotIn('PowerShell\\7', environment[keys[0]])
        self.assertEqual(environment['KEEP_OTHER_ENV'], 'unchanged')

    def test_healthy_microsoft_tool_and_enabled_defender(self):
        with patch.object(scan, '_run', return_value=command_result(json.dumps(healthy_status()))):
            evidence = []
            self.assertEqual(scan._status(Path('tool'), evidence), healthy_status())
            self.assertEqual(len(evidence), 1)

    def test_untrusted_tool_disabled_protection_and_missing_versions_fail(self):
        for key, value in (('ToolSignatureStatus', 'NotSigned'), ('ToolSigner', 'O=Other'),
                           ('AMServiceEnabled', False), ('AntivirusEnabled', 'True'),
                           ('RealTimeProtectionEnabled', False), ('AMRunningMode', 'Passive'),
                           ('AntivirusSignatureVersion', None), ('AntivirusSignatureAge', 4294967295)):
            status = healthy_status()
            status[key] = value
            with self.subTest(key=key), patch.object(scan, '_run', return_value=command_result(json.dumps(status))):
                with self.assertRaises(scan.DefenderScanError):
                    scan._status(Path('tool'), [])

    def test_status_errors_invalid_json_or_stderr_fail(self):
        for result in (command_result('{}', 1), command_result('invalid'), command_result('[]'),
                       command_result(json.dumps(healthy_status()), stderr='error')):
            with self.subTest(result=result), patch.object(scan, '_run', return_value=result):
                with self.assertRaises(scan.DefenderScanError):
                    scan._status(Path('tool'), [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base / 'release'
        self.directory.mkdir()
        (self.directory / 'source.txt').write_text('readable source', encoding='utf-8')
        self.report = self.base / 'scan.json'
        self.tool = self.base / 'scanner-placeholder.txt'
        self.tool.write_text('mock tool, never executed', encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(scan.sys, 'platform', 'win32').start()
        patch.object(scan, 'find_defender', return_value=self.tool).start()
        patch.object(scan, '_status', return_value=healthy_status()).start()
        self.run = patch.object(scan, '_run', return_value=command_result(clean_output(self.directory))).start()

    def test_clean_scan_uses_documented_custom_flags_and_binds_contents(self):
        result = scan.scan_directory(self.directory, self.report)
        self.assertTrue(result['confirmed_clean'])
        self.assertEqual(result, json.loads(self.report.read_text(encoding='utf-8')))
        self.assertEqual(list(map(str, self.run.call_args.args[0])),
                         [str(self.tool), '-Scan', '-ScanType', '3', '-File', str(self.directory), '-DisableRemediation'])
        self.assertEqual(result['files_sha256'], scan.inventory(self.directory))
        self.assertEqual(result['tool']['sha256'], scan._digest(self.tool))

    def test_zero_exit_with_detection_overwrites_prior_success_report(self):
        self.report.write_text('{"confirmed_clean": true}', encoding='utf-8')
        self.run.return_value = command_result('Scan starting...\nScan finished.\nThreat remediated.')
        with self.assertRaises(scan.DefenderScanError) as raised:
            scan.scan_directory(self.directory, self.report)
        self.assertFalse(raised.exception.result['confirmed_clean'])
        self.assertFalse(json.loads(self.report.read_text(encoding='utf-8'))['confirmed_clean'])

    def test_file_mutation_during_scan_invalidates_clean_output(self):
        def mutate(*args, **kwargs):
            (self.directory / 'source.txt').write_text('changed', encoding='utf-8')
            return command_result(clean_output(self.directory))
        self.run.side_effect = mutate
        with self.assertRaisesRegex(scan.DefenderScanError, 'changed or disappeared'):
            scan.scan_directory(self.directory, self.report)

    def test_file_quarantined_during_scan_cannot_pass(self):
        def remove(*args, **kwargs):
            (self.directory / 'source.txt').unlink()
            return command_result(clean_output(self.directory))
        self.run.side_effect = remove
        with self.assertRaises(scan.DefenderScanError):
            scan.scan_directory(self.directory, self.report)

    def test_scan_tool_replacement_invalidates_clean_output(self):
        def replace_tool(*args, **kwargs):
            self.tool.write_text('replaced tool', encoding='utf-8')
            return command_result(clean_output(self.directory))
        self.run.side_effect = replace_tool
        with self.assertRaisesRegex(scan.DefenderScanError, 'executable changed'):
            scan.scan_directory(self.directory, self.report)

    def test_protection_loss_after_scan_invalidates_clean_output(self):
        with patch.object(scan, '_status', side_effect=[healthy_status(), scan.DefenderScanError('Protection disabled')]):
            with self.assertRaisesRegex(scan.DefenderScanError, 'Protection disabled'):
                scan.scan_directory(self.directory, self.report)
        self.assertFalse(json.loads(self.report.read_text(encoding='utf-8'))['confirmed_clean'])

    def test_report_inside_target_is_rejected_without_writing_or_scanning(self):
        report = self.directory / 'result.json'
        with self.assertRaisesRegex(scan.DefenderScanError, 'outside'):
            scan.scan_directory(self.directory, report)
        self.assertFalse(report.exists())
        self.run.assert_not_called()

    def test_scanner_error_does_not_pass_even_with_clean_text(self):
        self.run.return_value.update(timed_out=True, error='timeout')
        with self.assertRaisesRegex(scan.DefenderScanError, 'timeout'):
            scan.scan_directory(self.directory, self.report)

    def test_unexpected_failure_and_interruption_do_not_leave_old_success(self):
        for error in (RuntimeError('unexpected'), KeyboardInterrupt()):
            self.report.write_text('{"confirmed_clean": true}', encoding='utf-8')
            self.run.side_effect = error
            with self.subTest(error=error), self.assertRaises((scan.DefenderScanError, KeyboardInterrupt)):
                scan.scan_directory(self.directory, self.report)
            self.assertFalse(json.loads(self.report.read_text(encoding='utf-8'))['confirmed_clean'])

    def test_empty_inventory_cannot_be_approved(self):
        (self.directory / 'source.txt').unlink()
        with self.assertRaisesRegex(scan.DefenderScanError, 'empty'):
            scan.scan_directory(self.directory, self.report)
        self.run.assert_not_called()

    def test_reparse_points_are_not_followed(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with patch.object(Path, 'lstat', return_value=info), self.assertRaisesRegex(scan.DefenderScanError, 'reparse'):
            scan._not_link(self.directory)


if __name__ == '__main__':
    unittest.main()
