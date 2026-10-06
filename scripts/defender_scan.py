"""Release-only Defender scan gate; a clean result is not an AV guarantee.

Uses the documented custom scan without remediation. File exclusions are
ignored and detections are reported on stdout, not in the event log/UI:
https://learn.microsoft.com/en-us/defender-endpoint/command-line-arguments-microsoft-defender-antivirus
No protection settings, exclusions, signatures or quarantine items are changed.
The intentionally strict output parser currently supports English only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class DefenderScanError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _decode(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode(locale.getencoding(), errors='replace')


def _run(command, *, timeout, env=None):
    result = {'command': list(map(str, command)), 'started_utc': _now(),
              'returncode': None, 'timed_out': False}
    stdout = stderr = b''
    try:
        completed = subprocess.run(result['command'], capture_output=True,
                                   timeout=timeout, env=env, shell=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        stdout, stderr = completed.stdout, completed.stderr
        result['returncode'] = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout or b'', error.stderr or b''
        result['timed_out'] = True
        result['error'] = 'Command timed out; scan completion is unconfirmed'
    except OSError as error:
        result['error'] = f'{type(error).__name__}: {error}'
    result.update(finished_utc=_now(), stdout=_decode(stdout), stderr=_decode(stderr),
                  stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                  stderr_sha256=hashlib.sha256(stderr).hexdigest())
    return result


def find_defender():
    """Use the newest installed platform, never PATH or the working directory."""
    platform = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'Microsoft/Windows Defender/Platform'
    candidates = []
    if platform.is_dir():
        for folder in platform.iterdir():
            if re.fullmatch(r'\d+(?:\.\d+)+(?:-\d+)?', folder.name):
                candidate = folder / 'MpCmdRun.exe'
                if candidate.is_file():
                    candidates.append((tuple(map(int, re.findall(r'\d+', folder.name))), candidate))
    if candidates:
        return max(candidates, key=lambda pair: pair[0])[1].resolve()
    fallback = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Windows Defender/MpCmdRun.exe'
    if fallback.is_file():
        return fallback.resolve()
    raise DefenderScanError('Microsoft Defender MpCmdRun.exe was not found')


STATUS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$scanToolPath = [Environment]::GetEnvironmentVariable('DUNGEONS_DEFENDER_SCAN_TOOL')
$signature = Get-AuthenticodeSignature -LiteralPath $scanToolPath
$status = Get-MpComputerStatus
[ordered]@{
    AMServiceEnabled = $status.AMServiceEnabled
    AntivirusEnabled = $status.AntivirusEnabled
    RealTimeProtectionEnabled = $status.RealTimeProtectionEnabled
    AMRunningMode = $status.AMRunningMode
    AMEngineVersion = $status.AMEngineVersion
    AMProductVersion = $status.AMProductVersion
    AntivirusSignatureVersion = $status.AntivirusSignatureVersion
    AntivirusSignatureAge = $status.AntivirusSignatureAge
    AntivirusSignatureLastUpdated = $status.AntivirusSignatureLastUpdated.ToUniversalTime().ToString('o')
    ToolSignatureStatus = [string]$signature.Status
    ToolSigner = $signature.SignerCertificate.Subject
    ToolVersion = (Get-Item -LiteralPath $scanToolPath).VersionInfo.FileVersion
} | ConvertTo-Json -Compress
"""


def _status(tool, evidence):
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # A pwsh 7 parent can otherwise make Windows PowerShell 5 load incompatible
    # modules. Also exclude user/custom modules from this security-status query.
    env = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
    env.update(DUNGEONS_DEFENDER_SCAN_TOOL=str(tool), PSModulePath=str(powershell.parent / 'Modules'))
    record = _run([powershell, '-NoProfile', '-NonInteractive', '-Command', STATUS_SCRIPT], timeout=45, env=env)
    evidence.append(record)
    if record['returncode'] != 0 or record.get('error') or record['stderr'].strip():
        raise DefenderScanError('Could not confirm Defender status and scan-tool signature')
    try:
        result = json.loads(record['stdout'])
    except (ValueError, TypeError) as error:
        raise DefenderScanError('Defender status was not valid JSON') from error
    if not isinstance(result, dict):
        raise DefenderScanError('Defender status was not an object')
    for key in ('AMServiceEnabled', 'AntivirusEnabled', 'RealTimeProtectionEnabled'):
        if result.get(key) is not True:
            raise DefenderScanError(f'Defender protection is not confirmed enabled: {key}')
    if result.get('AMRunningMode') != 'Normal':
        raise DefenderScanError('Defender is not confirmed in Normal mode')
    if result.get('ToolSignatureStatus') != 'Valid' or not re.search(
            r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', str(result.get('ToolSigner', ''))):
        raise DefenderScanError('MpCmdRun does not have a valid Microsoft Authenticode signature')
    for key in ('AMEngineVersion', 'AMProductVersion', 'AntivirusSignatureVersion',
                'AntivirusSignatureLastUpdated', 'ToolVersion'):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise DefenderScanError(f'Defender version information missing: {key}')
    age = result.get('AntivirusSignatureAge')
    if type(age) is not int or not 0 <= age < 4294967295:
        raise DefenderScanError('Defender signature age is unavailable')
    return result


def _not_link(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise DefenderScanError(f'Release scan does not follow links or reparse points: {path}')


def inventory(directory):
    """Bind the result to every ordinary file; missing/changed files fail closed."""
    _not_link(directory)
    files = {}
    def walk_error(error):
        raise error

    for folder, dirs, names in os.walk(directory, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + names):
            path = Path(folder) / name
            _not_link(path)
            if name in names:
                if not path.is_file():
                    raise DefenderScanError(f'Non-regular release file: {path}')
                files[path.relative_to(directory).as_posix()] = _digest(path)
    if not files:
        raise DefenderScanError('Refusing to approve an empty release directory')
    return dict(sorted(files.items()))


def parse_scan_output(returncode, stdout, stderr, directory):
    """Reject exit 0 without an explicit clean, completed English scan result.

    Microsoft also assigns exit 0 to successful remediation, so the code alone
    cannot approve a release. Unknown/localized/additional output is unconfirmed.
    """
    if returncode != 0:
        raise DefenderScanError(f'Defender scan did not succeed (exit {returncode})')
    if stderr.strip():
        raise DefenderScanError('Defender scan wrote diagnostic stderr')
    lines = [line.strip().casefold() for line in stdout.splitlines() if line.strip()]
    clean = {'no threats found.', 'no threats found'}
    target = str(directory).casefold()
    clean.update(f'Scanning {path} found no threats.'.casefold()
                 for path in (target, f'"{target}"', f"'{target}'"))
    if (len(lines) != 3 or lines[0] != 'scan starting...' or lines[1] != 'scan finished.'
            or lines[2] not in clean):
        raise DefenderScanError('Defender did not explicitly confirm a completed, threat-free scan; inspect the saved output')
    return True


def scan_directory(path, report, *, timeout=900):
    """Write evidence and return it only on confirmed clean, unchanged contents.

    The report must be outside the scanned directory. Failures raise
    DefenderScanError and retain a report with confirmed_clean=False.
    """
    report = Path(report).resolve()
    proposed_directory = Path(path).resolve()
    if report == proposed_directory or proposed_directory in report.parents:
        raise DefenderScanError('The report must be outside the scanned release directory')
    result = {'schema': 1, 'started_utc': _now(), 'confirmed_clean': False,
              'scope': 'This directory, signatures and time only; not a guarantee of safety.',
              'commands': []}
    # Invalidate an older success before doing work, including on interruption.
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    failure = None
    try:
        if sys.platform != 'win32':
            raise DefenderScanError('Microsoft Defender release scans require Windows')
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise DefenderScanError('Scan timeout must be between 1 and 3600 seconds')
        path = Path(path).absolute()
        _not_link(path)
        directory = path.resolve(strict=True)
        result['directory'] = str(directory)
        if not directory.is_dir() or directory == directory.parent:
            raise DefenderScanError('Scan target must be a release directory, not a drive root')
        tool = find_defender()
        result['tool'] = {'path': str(tool), 'sha256': _digest(tool)}
        result['defender_before'] = _status(tool, result['commands'])
        result['files_sha256'] = inventory(directory)
        result['inventory_sha256'] = hashlib.sha256(json.dumps(
            result['files_sha256'], sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        command = [tool, '-Scan', '-ScanType', '3', '-File', directory, '-DisableRemediation']
        scan = _run(command, timeout=timeout)
        result['commands'].append(scan)
        result['scan'] = scan
        if scan.get('error') or scan.get('timed_out'):
            raise DefenderScanError(scan.get('error', 'Defender scan completion is unconfirmed'))
        parse_scan_output(scan['returncode'], scan['stdout'], scan['stderr'], directory)
        result['defender_after'] = _status(tool, result['commands'])
        if inventory(directory) != result['files_sha256']:
            raise DefenderScanError('Release contents changed or disappeared during the scan')
        if _digest(tool) != result['tool']['sha256']:
            raise DefenderScanError('Defender scan executable changed during the scan')
        result['confirmed_clean'] = True
    except Exception as error:
        failure = error
        result['error'] = f'{type(error).__name__}: {error}'
    result['finished_utc'] = _now()
    # Even failed attempts replace an older success report, preventing stale reuse.
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    if failure is not None:
        raise DefenderScanError(f'Defender release gate failed: {failure}. Report: {report}', result) from failure
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    try:
        result = scan_directory(args.directory, args.report)
    except DefenderScanError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    print(f"Defender confirmed no threats for {len(result['files_sha256'])} unchanged files. Report: {args.report}")
