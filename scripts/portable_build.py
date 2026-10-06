"""Package readable mod sources with a pinned official Python/Tk runtime.

Retained runtime files keep their original bytes. Unused package-installation
tools are omitted, with their original hashes recorded for review.
No binary patching, download-at-launch or antivirus policy changes.
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

from build_info import APP_NAME, APP_VERSION, PACKAGE_NAME, START_FILENAME, GAME_BUILD, GAME_SHA256
from build_release import ROOT, RUNTIME, archive, digest, make_icon
from defender_scan import scan_directory
from launcher import SUPPORT_URL, DOWNLOADS_URL, COMPATIBILITY_INTERVAL
from executable_identity import REFERENCE_CANONICAL_SHA256, REFERENCE_PAYLOAD_SIZE


START_COMMAND = r'''@echo off
setlocal
if not exist "%~dp0runtime\pythonw.exe" goto missing
if not exist "%~dp0scripts\app.py" goto missing
start "Dungeons II - First & Third Person" "%~dp0runtime\pythonw.exe" -I -S -B "%~dp0scripts\app.py"
exit /b 0
:missing
echo Please extract the entire ZIP before starting Dungeons II Camera Mod.
echo Keep this file, the runtime folder and the scripts folder together.
pause
exit /b 1
'''


# This app uses only the standard library and never installs packages. Omit
# entire installer components, including ensurepip's nested wheel, rather than
# carrying unused executable launchers into an end-user runtime. These paths
# belong to the pinned Python 3.14.0 archive; a changed runtime needs review.
RUNTIME_OMIT_PREFIXES = (
    'Lib/site-packages/pip/',
    'Lib/site-packages/pip-25.2.dist-info/',
    'Lib/ensurepip/',
)


def omitted_runtime_file(name):
    return any(name.startswith(prefix) for prefix in RUNTIME_OMIT_PREFIXES)


def extract_runtime(archive_path, target):
    """Validate every member; extract the runtime without unused installers."""
    target.mkdir(parents=True)
    omitted = {}
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
        for entry in package.infolist():
            if omitted_runtime_file(entry.filename):
                if not entry.is_dir():
                    omitted[entry.filename] = hashlib.sha256(package.read(entry)).hexdigest()
            else:
                package.extract(entry, target)
    if any(not any(name.startswith(prefix) for name in omitted)
           for prefix in RUNTIME_OMIT_PREFIXES):
        raise RuntimeError('Review changed package-installation paths in the pinned runtime.')
    # Retained runtime files are copied without rewriting their bytes.
    runtime = target / 'python' if (target / 'python/python.exe').is_file() else target
    for name in ('python.exe', 'pythonw.exe', 'python314.dll', 'Lib/os.py',
                 'DLLs/_tkinter.pyd', 'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl'):
        if not (runtime / name).is_file():
            raise RuntimeError(f'Official runtime is incomplete: {name}')
    return runtime, omitted


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
    app = work / 'app' / PACKAGE_NAME
    app.mkdir(parents=True)
    runtime, omitted = extract_runtime(args.runtime_archive, app / 'runtime')
    # A root-level official runtime is expected by the simple launch command.
    if runtime != app / 'runtime':
        raise RuntimeError('Review the changed official runtime ZIP layout before packaging.')
    signatures = verify_signatures(runtime)
    (app / 'RUNTIME_OMISSIONS.json').write_text(json.dumps({
        'schema': 1, 'upstream_archive_sha256': lock['sha256'],
        'reason': 'The app uses only the standard library; pip and ensurepip are unused package-installation tools.',
        'omitted_prefixes': list(RUNTIME_OMIT_PREFIXES),
        'omitted_files_sha256': omitted,
        'retained_files': 'Original upstream bytes; no runtime executable was patched or renamed.'
    }, indent=2) + '\n', encoding='utf-8')
    (app / 'scripts').mkdir()
    for module in RUNTIME:
        shutil.copy2(ROOT / 'scripts' / (module + '.py'), app / 'scripts' / (module + '.py'))
    (app / 'release').mkdir()
    make_icon(app / 'release/app.ico')
    (app / START_FILENAME).write_text(START_COMMAND, encoding='utf-8', newline='\r\n')
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
        'game_certificate_equivalence': {
            'canonical_sha256': REFERENCE_CANONICAL_SHA256,
            'payload_bytes': REFERENCE_PAYLOAD_SIZE,
            'ignored_fields': 'PE checksum, certificate directory, validated unmapped certificate tail only',
            'all_other_bytes_must_match': True, 'other_store_gameplay_validated': False},
        'packaging': 'readable source with official CPython/Tk runtime; unused pip and ensurepip omitted; retained files unmodified',
        'entry_point': START_FILENAME,
        'python': lock['version'], 'runtime_origin': lock, 'runtime_signatures': signatures,
        'runtime_omissions': {'manifest': 'RUNTIME_OMISSIONS.json',
                              'file_count': len(omitted),
                              'prefixes': list(RUNTIME_OMIT_PREFIXES)},
        'support': {'url': SUPPORT_URL, 'optional': True, 'opens_in_default_browser_on_click': True},
        'compatibility_check': {'kind': 'running game, selected executable, then installed Steam copies', 'interval_seconds': COMPATIBILITY_INTERVAL,
                                'network_requests': False, 'automatic_repair': False,
                                'downloads_page': DOWNLOADS_URL, 'running_executable_guard': 'Win64 full SHA256 or certificate-only equivalent; WinGDK pinned mapped engine/game code, unwind table, PE layout and native guards through read-only access before requesting write access; process identity rechecked',
                                'report_export': 'user-requested JSON/clipboard plus latest local running/selected build snapshot; no installation paths, process IDs or account details; no upload'},
        'launcher_support': {'launch_parent_required': False,
                             'recognized_process_names': ['Dungeons-Win64-Shipping.exe', 'Dungeons-WinGDK-Shipping.exe'],
                             'process_discovery': 'bounded Windows Unicode Toolhelp32 snapshot',
                             'validated_reference': GAME_BUILD,
                             'xbox_profile': 'GDK 1.1.1.0; read-only setup passed and local walking, arm turning, armor toggles and menu/camera-transition tests confirmed; broader gameplay testing pending',
                             'other_store_gameplay': 'Xbox/Minecraft Launcher/Microsoft Store share the measured GDK 1.1.1.0 profile. Epic unverified; unknown separately compiled editions and future builds remain blocked.'},
        'offline_smoke_test': smoke,
        'source_sha256': {name + '.py': digest(app / 'scripts' / (name + '.py')) for name in RUNTIME},
        'defender_before_execution': scan_summary(pre_scan),
        'gameplay_baseline': 'Test.10 direction with public-beta.3 input fixes, plus preview shoulder camera and opt-in sprint; native roll experiments remain disabled',
        'preview_features': {
            'movement_delivery': 'Separate process supplies freshly sampled physical movement to the consumed native input vector; 2 ms polling target; 75 ms main-loop lease; focus/ownership/mapping/lock guards; acknowledged pause and joined shutdown; local Xbox walking improvement confirmed, broader validation pending',
            'arm_turning': 'Camera, visible-arm heading and current local view angle refresh together between slower binding/combat/scene checks. Mouse deltas are fresh; stick elapsed time is integrated once. Enabled by default. Local Xbox tester confirmed stationary turn snapping gone and walking normal; broader-session validation pending. Renderer-refresh and native-facing experiments were rejected and reverted.',
            'controller_dodge': 'R3 gameplay dodge in both perspectives; axis-only suppression survives partial/duplicate runtime maps, retained-camera menus and immediate resets; normal-camera exit restores original controls',
            'third_person': 'F3 or triple-click L3 while custom camera active; adjustable shoulder position, FOV and eased transitions',
            'shoulder_position': 'native additive view offset from a stable eye-height anchor; avoids pawn-yaw orbit and sampled world-position rewinds',
            'shoulder_swap': 'F2 smoothly swaps left/right in focused third-person gameplay; optional Helper switch; chosen side survives menu/camera recreation within the session',
            'villager_menus': 'quest, Enchantsmith, merchant and purchase-preview menus retain the same camera session and selected perspective',
            'airborne_mouse': 'released-then-clicked LMB routes native HeavyJumpAttack while falling; preserves native Q/side-button and ground click guard',
            'collision': 'no automatic shoulder switching or world collision probe; native isometric arm cannot detect shoulder obstructions; use F2 manually',
            'sprint': 'off by default; guarded active native walking parameter only; 1.05-1.35 multiplier; native speed outputs, attributes, acceleration and falling settings are read-only',
            'controls': 'instant R3 dodge retained; single L3/Shift sprint, double L3/Shift quick turn, hold L3 camera toggle',
            'sprint_refresh': 'native code computes walking and minimum analog speed; rapid parameter contention pauses sprint with a visible notice; no repeated output-speed writes',
            'dodge_while_attacking': 'fresh third-person R/R3 movement request briefly takes facing priority before attack; native roll/lock/ranged handoff retain control; no delayed or synthetic input',
            'live_validation': 'user reports preview.3 mostly smooth but solo sprint jitters and attack rolls face forward; native sprint writer and unlocked attack/roll sequence checked read-only; preview.4 changes need gameplay acceptance',
            'armor_visibility': 'Independent helmet/chest/pants/boots and all controls, default visible. Optional inventory Y/Triangle toggles the highlighted equipped piece; all four pieces locally confirmed on Xbox. Session-only choices share visibility ownership across camera modes; F5/Helper shutdown restores native flags. No inventory/stats/save changes.',
            'other_stores': 'Xbox/Minecraft Launcher/Microsoft Store GDK 1.1.1.0 preview profile; read-only setup and local walking/armor tests passed; local arm-turning and menu/armor-transition tests also passed; broader gameplay acceptance is pending'},
        'input_fixes': {'menus': 'validated local UI presentation and native cursor handoff',
                        'click_movement': 'suppress native empty-ground click-release path selection',
                        'executable_patches': False,
                        'live_validation': {'menus': 'user confirmed mouse control and first-person resumption',
                                            'click_movement': 'pending; automated tests and live read-only guards passed'}},
        'multiplayer_validation': 'Guest backward rolls and normal attacks user-confirmed. Keyboard-specific, host/observer and long-session acceptance pending; prior crash/food event unresolved.',
        'native_roll_prototype': {'available': False},
        'roll_target_data_experiment': {'available': False},
        'roll_direction': {'strategy': 'movement-facing before native commitment; third-person R uses native facing-based dodge after physical key release', 'late_force_writes': False},
        'previous_release_detection': {'version': '0.2.0-public-beta.1', 'status': 'retired; Defender Wacatac detection unresolved; executable not reused'},
        'antivirus_validation': 'Build requires explicit clean Defender scans before execution and before export; scan receipts accompany release. A point-in-time result is not a universal safety guarantee.'
    }
    (app / 'BUILD.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    files = {p.relative_to(app).as_posix(): digest(p) for p in sorted(app.rglob('*')) if p.is_file()}
    (app / 'FILES.sha256').write_text(''.join(f'{h}  {name}\n' for name, h in files.items()), encoding='utf-8')
    final_app_scan = scan_directory(app, work / 'defender-final-application.json')
    source = work / f'{PACKAGE_NAME}-Source'
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
    binary_zip = downloads / f'{PACKAGE_NAME}-{APP_VERSION}-Windows-x64.zip'
    source_zip = downloads / f'{PACKAGE_NAME}-{APP_VERSION}-Source.zip'
    archive(app, binary_zip)
    archive(source, source_zip)
    print('Checking both final ZIP archives with Defender...', flush=True)
    final_archive_scan = scan_directory(downloads, work / 'defender-final-archives.json')
    receipt = downloads / f'{PACKAGE_NAME}-{APP_VERSION}-Defender-scan.json'
    receipt.write_text(json.dumps({'version': APP_VERSION,
        'application_scan': scan_summary(final_app_scan),
        'archive_scan': scan_summary(final_archive_scan),
        'archives_sha256': final_archive_scan['files_sha256'],
        'note': 'No threats detected by Microsoft Defender with these signatures at these times. This does not clear public-beta.1 or guarantee every future antivirus result.'}, indent=2) + '\n', encoding='utf-8')
    checksums = downloads / f'{PACKAGE_NAME}-{APP_VERSION}-SHA256.txt'
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
