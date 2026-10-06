"""Package readable mod sources with a pinned official Python/Tk runtime.

No repacking, binary patching, download-at-launch or antivirus policy changes.
The original public-beta.1 executable is retired, not restored or reused.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile

from build_info import APP_NAME, APP_VERSION, GAME_BUILD, GAME_SHA256
from build_release import ROOT, RUNTIME, archive, digest, make_icon
from defender_scan import scan_directory
from launcher import SUPPORT_URL, DOWNLOADS_URL, COMPATIBILITY_INTERVAL


START_COMMAND = r'''@echo off
setlocal
if not exist "%~dp0runtime\pythonw.exe" goto missing
if not exist "%~dp0scripts\app.py" goto missing
start "Dungeons First Person" "%~dp0runtime\pythonw.exe" -I -S -B "%~dp0scripts\app.py"
exit /b 0
:missing
echo Please extract the entire ZIP before starting Dungeons First Person.
echo Keep this file, the runtime folder and the scripts folder together.
pause
exit /b 1
'''


def extract_runtime(archive_path, target):
    """Extract only a plain, uniquely named tree inside the new build folder."""
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as package:
        seen = set()
        for entry in package.infolist():
            name = entry.filename
            relative = PurePosixPath(name)
            if (not name or relative.is_absolute() or '..' in relative.parts or
                    '\\' in name or ':' in name or
                    relative.as_posix() != name.rstrip('/') or
                    name.casefold() in seen or stat.S_ISLNK(entry.external_attr >> 16)):
                raise RuntimeError(f'Unsafe or duplicate runtime path: {name}')
            seen.add(name.casefold())
            if not (target / name).resolve().is_relative_to(target.resolve()):
                raise RuntimeError('Runtime extraction escaped the build folder')
        if package.testzip() is not None:
            raise RuntimeError('Official runtime ZIP failed its CRC check')
        package.extractall(target)
    # Keep the official archive's python/ layout intact, without rewriting bytes.
    runtime = target / 'python' if (target / 'python/python.exe').is_file() else target
    for name in ('python.exe', 'pythonw.exe', 'python314.dll', 'Lib/os.py',
                 'DLLs/_tkinter.pyd', 'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl'):
        if not (runtime / name).is_file():
            raise RuntimeError(f'Official runtime is incomplete: {name}')
    return runtime


def verify_signatures(runtime):
    paths = [str(runtime / name) for name in ('python.exe', 'pythonw.exe', 'python314.dll')]
    command = """$ErrorActionPreference='Stop';
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json;
@($paths | ForEach-Object {
  $s = Get-AuthenticodeSignature -LiteralPath $_;
  [pscustomobject]@{name=[IO.Path]::GetFileName($_);status=$s.Status.ToString();
    subject=$s.SignerCertificate.Subject;thumbprint=$s.SignerCertificate.Thumbprint}
}) | ConvertTo-Json -Compress
"""
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
    environment['PSModulePath'] = str(powershell.parent / 'Modules')
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                            input=json.dumps(paths), text=True, capture_output=True,
                            env=environment,
                            check=True, timeout=60)
    signatures = json.loads(result.stdout)
    if len(signatures) != len(paths) or any(
            row['status'] != 'Valid' or 'Python Software Foundation' not in row['subject']
            for row in signatures):
        raise RuntimeError('Official Python executable signatures did not validate')
    return signatures


def smoke_test(runtime, app, work):
    reports = {}
    unrelated = work / 'unrelated working directory'
    unrelated.mkdir()
    hostile = work / 'unused environment path'
    environment = dict(os.environ, PYTHONHOME=str(hostile), PYTHONPATH=str(hostile),
                       TCL_LIBRARY=str(hostile), TK_LIBRARY=str(hostile), TCLLIBPATH=str(hostile))
    for executable in ('python.exe', 'pythonw.exe'):
        report = work / (executable + '-selftest.json')
        subprocess.run([str(runtime / executable), '-I', '-S', '-B',
                        str(app / 'scripts/app.py'), '--self-test', str(report)],
                       cwd=unrelated, env=environment, check=True, timeout=60)
        value = json.loads(report.read_text(encoding='utf-8'))
        if value.get('passed') is not True:
            raise RuntimeError(f'Packaged {executable} smoke test failed')
        reports[executable] = value
    return {'passed': True, 'interpreters': reports,
            'unrelated_working_directory': True, 'hostile_environment_ignored': True}


def scan_summary(result):
    """Public receipt without local paths, user names or command output."""
    status = result['defender_after']
    return {'confirmed_clean': result['confirmed_clean'],
            'started_utc': result['started_utc'], 'finished_utc': result['finished_utc'],
            'engine_version': status['AMEngineVersion'],
            'platform_version': status['AMProductVersion'],
            'signature_version': status['AntivirusSignatureVersion'],
            'file_count': len(result['files_sha256']),
            'inventory_sha256': result['inventory_sha256'],
            'protection_enabled': True, 'scope': result['scope']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-archive', type=Path,
                        default=ROOT / '.cache/runtime-public-beta2/python-3.14.0-amd64.zip')
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Build and scan on Windows with Microsoft Defender enabled.')
    lock = json.loads((ROOT / 'release/python-runtime.json').read_text(encoding='utf-8'))
    if digest(args.runtime_archive) != lock['sha256']:
        raise RuntimeError('Python runtime does not match the pinned official SHA256.')
    work = ROOT / '.cache/builds' / (time.strftime('%Y%m%d-%H%M%S') + '-portable-' + uuid.uuid4().hex[:8])
    app = work / 'app/DungeonsFirstPerson'
    app.mkdir(parents=True)
    runtime = extract_runtime(args.runtime_archive, app / 'runtime')
    # A root-level official runtime is expected by the simple launch command.
    if runtime != app / 'runtime':
        raise RuntimeError('Review the changed official runtime ZIP layout before packaging.')
    signatures = verify_signatures(runtime)
    (app / 'scripts').mkdir()
    for module in RUNTIME:
        shutil.copy2(ROOT / 'scripts' / (module + '.py'), app / 'scripts' / (module + '.py'))
    (app / 'release').mkdir()
    make_icon(app / 'release/app.ico')
    (app / 'Start Dungeons First Person.cmd').write_text(START_COMMAND, encoding='utf-8', newline='\r\n')
    for name in ('README.txt', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.txt',
                 'MULTIPLAYER_TESTING.md', 'NATIVE_ROLL_PROTOTYPE.md'):
        shutil.copy2(ROOT / 'release' / name, app / name)
    shutil.copy2(ROOT / 'LICENSE', app / 'LICENSE.txt')
    licenses = app / 'licenses'
    licenses.mkdir()
    shutil.copy2(runtime / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    shutil.copy2(runtime / 'tcl/tk8.6/license.terms', licenses / 'Tk-license.txt')
    for path in (ROOT / 'release/licenses').glob('*.txt'):
        shutil.copy2(path, licenses / path.name)
    print('Checking the complete application with Defender before running it...', flush=True)
    pre_scan = scan_directory(app, work / 'defender-before-execution.json')
    smoke = smoke_test(runtime, app, work)
    metadata = {
        'name': APP_NAME, 'version': APP_VERSION, 'platform': 'Windows x64',
        'game_build': GAME_BUILD, 'game_executable_sha256': GAME_SHA256,
        'packaging': 'readable source with unmodified official CPython/Tk runtime',
        'entry_point': 'Start Dungeons First Person.cmd',
        'python': lock['version'], 'runtime_origin': lock, 'runtime_signatures': signatures,
        'support': {'url': SUPPORT_URL, 'optional': True, 'opens_in_default_browser_on_click': True},
        'compatibility_check': {'kind': 'running game, selected executable, then installed Steam copies', 'interval_seconds': COMPATIBILITY_INTERVAL,
                                'network_requests': False, 'automatic_repair': False,
                                'downloads_page': DOWNLOADS_URL, 'running_executable_guard': 'full SHA256 before memory access; process identity rechecked',
                                'report_export': 'user-requested local JSON; no installation paths, process IDs or account details'},
        'launcher_support': {'launch_parent_required': False,
                             'recognized_process_names': ['Dungeons-Win64-Shipping.exe', 'Dungeons-WinGDK-Shipping.exe'],
                             'validated_reference': GAME_BUILD,
                             'other_store_gameplay': 'unvalidated; detection does not add compatible fingerprints'},
        'offline_smoke_test': smoke,
        'source_sha256': {name + '.py': digest(app / 'scripts' / (name + '.py')) for name in RUNTIME},
        'defender_before_execution': scan_summary(pre_scan),
        'gameplay_baseline': 'Test.10 direction with public-beta.3 input fixes; native roll experiments remain disabled',
        'input_fixes': {'menus': 'validated local UI presentation and native cursor handoff',
                        'click_movement': 'suppress native empty-ground click-release path selection',
                        'executable_patches': False,
                        'live_validation': {'menus': 'user confirmed mouse control and first-person resumption',
                                            'click_movement': 'pending; automated tests and live read-only guards passed'}},
        'multiplayer_validation': 'Guest backward rolls and normal attacks user-confirmed. Keyboard-specific, host/observer and long-session acceptance pending; prior crash/food event unresolved.',
        'native_roll_prototype': {'available': False},
        'roll_target_data_experiment': {'available': False},
        'roll_direction': {'strategy': 'movement-facing before native commitment', 'late_force_writes': False},
        'previous_release_detection': {'version': '0.2.0-public-beta.1', 'status': 'retired; Defender Wacatac detection unresolved; executable not reused'},
        'antivirus_validation': 'Build requires explicit clean Defender scans before execution and before export; scan receipts accompany release. A point-in-time result is not a universal safety guarantee.'
    }
    (app / 'BUILD.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    files = {p.relative_to(app).as_posix(): digest(p) for p in sorted(app.rglob('*')) if p.is_file()}
    (app / 'FILES.sha256').write_text(''.join(f'{h}  {name}\n' for name, h in files.items()), encoding='utf-8')
    final_app_scan = scan_directory(app, work / 'defender-final-application.json')
    source = work / 'DungeonsFirstPerson-Source'
    (source / 'scripts').mkdir(parents=True)
    (source / 'tests').mkdir()
    for name in RUNTIME:
        shutil.copy2(app / 'scripts' / (name + '.py'), source / 'scripts' / (name + '.py'))
    for name in ('build_release', 'portable_build', 'defender_scan'):
        shutil.copy2(ROOT / 'scripts' / (name + '.py'), source / 'scripts' / (name + '.py'))
    for path in (ROOT / 'tests').glob('test_*.py'):
        if path.name not in ('test_installer.py', 'test_terrain_test_install.py'):
            shutil.copy2(path, source / 'tests' / path.name)
    shutil.copytree(ROOT / 'release', source / 'release')
    shutil.copy2(ROOT / 'LICENSE', source / 'LICENSE')
    shutil.copy2(ROOT / 'release/BUILDING.md', source / 'README.md')
    downloads = work / 'downloads'
    downloads.mkdir()
    binary_zip = downloads / f'DungeonsFirstPerson-{APP_VERSION}-Windows-x64.zip'
    source_zip = downloads / f'DungeonsFirstPerson-{APP_VERSION}-Source.zip'
    archive(app, binary_zip)
    archive(source, source_zip)
    print('Checking both final ZIP archives with Defender...', flush=True)
    final_archive_scan = scan_directory(downloads, work / 'defender-final-archives.json')
    receipt = downloads / f'DungeonsFirstPerson-{APP_VERSION}-Defender-scan.json'
    receipt.write_text(json.dumps({'version': APP_VERSION,
        'application_scan': scan_summary(final_app_scan),
        'archive_scan': scan_summary(final_archive_scan),
        'archives_sha256': final_archive_scan['files_sha256'],
        'note': 'No threats detected by Microsoft Defender with these signatures at these times. This does not clear public-beta.1 or guarantee every future antivirus result.'}, indent=2) + '\n', encoding='utf-8')
    checksums = downloads / f'DungeonsFirstPerson-{APP_VERSION}-SHA256.txt'
    checksums.write_text(''.join(f'{digest(p)}  {p.name}\n' for p in (binary_zip, source_zip, receipt)), encoding='utf-8')
    # Reports stay outside the hashed/scanned directories; only export on success.
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    for path in downloads.iterdir():
        if (output / path.name).exists():
            raise RuntimeError(f'Refusing to overwrite a released artifact: {path.name}')
    for name, expected in final_archive_scan['files_sha256'].items():
        if digest(downloads / name) != expected:
            raise RuntimeError(f'Archive changed after its Defender scan: {name}')
    for path in downloads.iterdir():
        shutil.copy2(path, output / path.name)
        expected = final_archive_scan['files_sha256'].get(path.name, digest(path))
        if digest(output / path.name) != expected or digest(path) != expected:
            raise RuntimeError(f'Exported artifact differs from verified bytes: {path.name}')
    result = {'binary_zip': str(output / binary_zip.name), 'source_zip': str(output / source_zip.name),
              'checksums': str(output / checksums.name), 'app': str(app),
              'defender_scan_receipt': str(output / receipt.name),
              'binary_bytes': binary_zip.stat().st_size, 'release_status': 'DEFENDER_SCAN_PASSED',
              'scan_reports': [str(work / name) for name in ('defender-before-execution.json',
                               'defender-final-application.json', 'defender-final-archives.json')]}
    (output / 'latest-build.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
