"""Read-only, launcher-neutral compatibility advisory. Never changes the guard.

Call GameCompatibilityChecker.check() from a background worker. Cached hashes
make repeated UI checks inexpensive; the helper still hashes its actual running
image independently before it can change game memory. No network or file writes.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import subprocess

from build_info import APP_VERSION, GAME_BUILD, GAME_EXE, GAME_SHA256


APP_ID = '1912410'
MAX_VDF_BYTES = 1024 * 1024
MAX_LIBRARIES = 64
MAX_GAME_PROCESSES = 16
MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
EXE_RELATIVE = Path('Dungeons') / 'Binaries' / 'Win64' / GAME_EXE
MAX_PE_SECTIONS = 96

# Static text is deliberately separate from local exception details. These
# codes can be copied by a player without disclosing Windows usernames/paths.
COMPATIBILITY_MESSAGES = {
    'DFP-OK': 'The executable matches the tested build. Start the Helper in a playable area.',
    'DFP-EQUIVALENT-BUILD': 'The executable matches the tested game code and data after excluding certificate-only differences. This edition has not been playtested; start the Helper in a playable area and copy support details if it fails.',
    'DFP-GDK-BUILD': 'The running Xbox/Minecraft Launcher game matches the supported Xbox preview code and layout. Start the Helper in a playable area.',
    'DFP-GDK-START': 'This Xbox/Minecraft Launcher executable is protected by Windows. Start the game from its usual launcher so the Helper can check the running copy. Folder ownership changes are not needed.',
    'DFP-UNTESTED-BUILD': 'This game executable is a different build. This Helper cannot safely apply it yet. Copy support details so this edition can be investigated.',
    'DFP-GAME-NOT-FOUND': 'Start the game from your usual launcher, wait for its main menu, then check again. You can also choose the game shipping executable.',
    'DFP-MULTIPLE-GAMES': 'More than one game copy is running. Close the extra copy, then check again.',
    'DFP-PROCESS-ACCESS': 'Windows would not let the Helper access the running game. Run the game and Helper as the same Windows user and at the same privilege level, then check again.',
    'DFP-PROCESS-IDENTITY': 'The running game could not be identified completely. Wait until the game has loaded, then check again and copy support details if it persists.',
    'DFP-PROCESS-CHANGED': 'The game closed, restarted or changed during the check. Wait until it has loaded, then check again.',
    'DFP-DISCOVERY-FAILED': 'The running-game check failed. Check again after the game loads; copy support details if it persists.',
    'DFP-FILE-ACCESS': 'Windows would not let the Helper read this game executable. Start the game from its usual launcher and check the running copy. Do not change protected-folder ownership; copy support details if it persists.',
    'DFP-FILE-MISSING': 'The game executable could not be found. Start your installed game and check again, or choose its current shipping executable.',
    'DFP-FILE-CHANGED': 'The executable changed during the check. Finish any game update, then check again.',
    'DFP-WRONG-EXE': 'Select the Dungeons shipping executable inside the game installation, rather than the Minecraft, Xbox, Steam or Epic launcher.',
    'DFP-SELECTION-MISMATCH': 'The selected game executable is not the copy currently running. Select the running copy or restart the game from the installation you selected.',
    'DFP-STEAM-METADATA': 'Steam installation information could not be verified. Start the game from your usual launcher and check its running executable instead.',
    'DFP-INSTALL-INCOMPLETE': 'The installation appears to be updating or incomplete. Finish the update or verification, then check again.',
    'DFP-MIXED-INSTALLS': 'Installed game copies disagree. Start the copy you want to play and check its running executable.',
    'DFP-CHECK-INCOMPLETE': 'Compatibility could not be confirmed. Start the game, check again, then copy support details if it persists.',
}
DEFAULT_REASON_CODES = {'compatible': 'DFP-OK', 'unsupported': 'DFP-UNTESTED-BUILD',
                        'not_found': 'DFP-GAME-NOT-FOUND', 'unknown': 'DFP-CHECK-INCOMPLETE'}


class CompatibilityUnknown(ValueError):
    def __init__(self, message, code='DFP-CHECK-INCOMPLETE'):
        super().__init__(message)
        self.code = code if code in COMPATIBILITY_MESSAGES else 'DFP-CHECK-INCOMPLETE'


def _reason_code(result):
    code = result.get('reason_code')
    state = result.get('state')
    return code if isinstance(code, str) and code in COMPATIBILITY_MESSAGES else DEFAULT_REASON_CODES.get(state if isinstance(state, str) else '', 'DFP-CHECK-INCOMPLETE')


def compatibility_message(result):
    """Return a stable, shareable diagnosis without local paths/errors."""
    code = _reason_code(result)
    return f'[{code}] {COMPATIBILITY_MESSAGES[code]}'


def _error_code(error, *, process=False, default='DFP-CHECK-INCOMPLETE'):
    if isinstance(error, CompatibilityUnknown):
        return error.code if error.code != 'DFP-CHECK-INCOMPLETE' else default
    explicit = getattr(error, 'reason_code', None)
    if isinstance(explicit, str) and explicit in COMPATIBILITY_MESSAGES:
        return explicit
    if isinstance(error, PermissionError) or getattr(error, 'winerror', None) == 5:
        return 'DFP-PROCESS-ACCESS' if process else 'DFP-FILE-ACCESS'
    if isinstance(error, FileNotFoundError):
        return 'DFP-PROCESS-CHANGED' if process else 'DFP-FILE-MISSING'
    return default


def _public_pe_metadata(value):
    """Whitelist numeric PE facts; never copy section names/version strings."""
    if not isinstance(value, dict):
        return None
    ranges = {'machine': (1, 0xffff), 'optional_magic': (1, 0xffff),
              'timestamp': (0, 0xffffffff), 'image_size': (1, 0xffffffff),
              'entrypoint_rva': (0, 0xffffffff), 'subsystem': (0, 0xffff),
              'section_count': (1, MAX_PE_SECTIONS)}
    result = {}
    for key, (low, high) in ranges.items():
        item = value.get(key)
        if type(item) is not int or not low <= item <= high:
            return None
        result[key] = item
    sections = value.get('sections')
    if not isinstance(sections, list) or len(sections) != result['section_count']:
        return None
    public_sections = []
    for section in sections:
        if not isinstance(section, dict):
            return None
        facts = {}
        for key in ('rva', 'virtual_size', 'raw_size', 'characteristics'):
            item = section.get(key)
            if type(item) is not int or not 0 <= item <= 0xffffffff:
                return None
            facts[key] = item
        public_sections.append(facts)
    result['sections'] = public_sections
    return result


def read_pe_metadata(executable, expected_identity):
    """Read at most 8 KiB of bounded PE headers for an advisory build report.

    No code, resources, embedded strings, or file paths enter the result. This
    metadata is not a compatibility decision. A non-PE fixture or unavailable
    metadata simply has no optional PE facts; callers still recheck identity.
    """
    try:
        with executable.open('rb') as stream:
            opened = file_identity(os.fstat(stream.fileno()))
            if opened[:4] != expected_identity[:4]:
                return None
            size = opened[2]
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b'MZ':
                return None
            offset = struct.unpack_from('<I', dos, 60)[0]
            if not 64 <= offset <= min(size - 24, 1024 * 1024):
                return None
            stream.seek(offset)
            coff = stream.read(24)
            if len(coff) != 24 or coff[:4] != b'PE\0\0':
                return None
            machine, count, timestamp = struct.unpack_from('<HHI', coff, 4)
            optional_size = struct.unpack_from('<H', coff, 20)[0]
            if not 1 <= count <= MAX_PE_SECTIONS or not 70 <= optional_size <= 4096:
                return None
            if offset + 24 + optional_size + count * 40 > size:
                return None
            optional = stream.read(optional_size)
            magic = struct.unpack_from('<H', optional)[0]
            if magic not in (0x10b, 0x20b):
                return None
            result = {'machine': machine, 'optional_magic': magic, 'timestamp': timestamp,
                      'image_size': struct.unpack_from('<I', optional, 56)[0],
                      'entrypoint_rva': struct.unpack_from('<I', optional, 16)[0],
                      'subsystem': struct.unpack_from('<H', optional, 68)[0],
                      'section_count': count, 'sections': []}
            for _ in range(count):
                header = stream.read(40)
                virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', header, 8)
                if raw_size and (raw_offset > size or raw_size > size - raw_offset):
                    return None
                result['sections'].append({'rva': rva, 'virtual_size': virtual_size, 'raw_size': raw_size,
                                           'characteristics': struct.unpack_from('<I', header, 36)[0]})
            if file_identity(os.fstat(stream.fileno())) != opened:
                return None
        if file_identity(executable.stat()) != expected_identity:
            return None
        return _public_pe_metadata(result)
    except (OSError, ValueError, struct.error):
        return None


def running_game_processes():
    # Import lazily so parser/file tests and the launcher can load this module
    # without touching Windows process APIs. Discovery uses query-only access.
    from game_memory import discover_game_processes
    return discover_game_processes()


def recognized_executable_names():
    from game_memory import GAME_PROCESS_NAMES
    return {name.casefold() for name in GAME_PROCESS_NAMES}


def compatibility_report(result):
    """Return shareable build facts without local paths, PIDs or raw errors.

    The launcher may keep a sanitized local snapshot or let the player copy or
    save one. No files are written and no network requests are made here. Hashes
    identify builds for investigation; reports never authorize a new build.
    """
    valid_states = {'compatible', 'unsupported', 'unknown', 'not_found'}
    valid_sources = {'running', 'selected', 'steam', 'none'}
    state = result.get('state') if isinstance(result.get('state'), str) and result.get('state') in valid_states else 'unknown'
    source = result.get('source') if isinstance(result.get('source'), str) and result.get('source') in valid_sources else 'none'
    checked = result.get('checked_utc', '')
    try:
        checked = datetime.fromisoformat(checked).isoformat()
    except (TypeError, ValueError):
        checked = None
    report = {'schema': 2, 'mod_version': APP_VERSION, 'state': state, 'source': source,
              'reason_code': _reason_code(result), 'message': compatibility_message(result),
              'checked_utc': checked, 'supported_reference_build': GAME_BUILD,
              'supported_sha256': GAME_SHA256, 'copies': [],
              'note': 'Local build identification only; this report does not change compatibility or upload anything.'}
    windows_error = result.get('windows_error')
    if type(windows_error) is int and 1 <= windows_error <= 65535:
        report['windows_error'] = windows_error
    helper_code = result.get('helper_reason_code')
    if isinstance(helper_code, str) and helper_code in COMPATIBILITY_MESSAGES:
        # Preserve an actual attach failure separately from an on-disk build
        # match. Query-only discovery may succeed even when VM access fails.
        report['helper_reason_code'] = helper_code
        report['helper_message'] = compatibility_message({'reason_code': helper_code})
    names = recognized_executable_names()
    copies = result.get('copies', [])
    for item in (copies if isinstance(copies, list) else [])[:MAX_LIBRARIES]:
        if not isinstance(item, dict):
            continue
        # Accept only recognized literal basenames. Never copy arbitrary path
        # segments, an exception message or a process-provided display name.
        raw = item.get('executable', '')
        name = raw.replace('\\', '/').rsplit('/', 1)[-1] if isinstance(raw, str) else ''
        if name.casefold() not in names:
            name = item.get('name', '')
        name = name if isinstance(name, str) and name.casefold() in names else 'unidentified'
        entry = {'executable_name': name,
                 'build_family': 'WinGDK' if '-wingdk-' in name.casefold() else
                 'Win64' if '-win64-' in name.casefold() else 'unknown',
                 'state': item.get('state') if isinstance(item.get('state'), str) and item.get('state') in valid_states else 'unknown',
                 'reason_code': _reason_code(item)}
        digest = item.get('sha256')
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            entry['sha256'] = digest.lower()
        identity = item.get('file_identity')
        size = identity.get('size') if isinstance(identity, dict) else None
        if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_EXECUTABLE_BYTES:
            entry['size'] = size
        windows_error = item.get('windows_error')
        if type(windows_error) is int and 1 <= windows_error <= 65535:
            entry['windows_error'] = windows_error
        match_kind = item.get('match_kind')
        if match_kind in ('exact', 'certificate_equivalent', 'mapped_code'):
            entry['match_kind'] = match_kind
        for field in ('mapped_code_sha256', 'unwind_sha256'):
            digest = item.get(field)
            if match_kind == 'mapped_code' and isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                entry[field] = digest.lower()
        steam_build = item.get('steam_build')
        if source == 'steam' and isinstance(steam_build, str) and re.fullmatch(r'\d{1,20}', steam_build):
            entry['steam_build'] = steam_build
        pe = _public_pe_metadata(item.get('pe'))
        if pe is not None:
            entry['pe'] = pe
        report['copies'].append(entry)
    return report


def parse_vdf(text):
    """Bounded KeyValues reader: quoted/bare strings, braces and // comments."""
    if len(text) > MAX_VDF_BYTES:
        raise CompatibilityUnknown('Steam metadata is too large')
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == '\ufeff':
            index += 1
            continue
        if text.startswith('//', index):
            end = text.find('\n', index)
            index = len(text) if end < 0 else end + 1
            continue
        if char in '{}':
            tokens.append((char, char))
            index += 1
        elif char == '"':
            index += 1
            value = []
            while index < len(text) and text[index] != '"':
                char = text[index]
                if char == '\\' and index + 1 < len(text) and text[index + 1] in ('\\', '"'):
                    index += 1
                    char = text[index]
                value.append(char)
                index += 1
            if index >= len(text):
                raise CompatibilityUnknown('Steam metadata has an unfinished quoted value')
            tokens.append(('text', ''.join(value)))
            index += 1
        else:
            start = index
            while index < len(text) and not text[index].isspace() and text[index] not in '{}"':
                if text.startswith('//', index):
                    break
                index += 1
            if index == start:
                raise CompatibilityUnknown('Invalid Steam metadata token')
            tokens.append(('text', text[start:index]))
        if len(tokens) > 40000:
            raise CompatibilityUnknown('Steam metadata contains too many entries')

    index = 0

    def object_value(depth, nested):
        nonlocal index
        if depth > 16:
            raise CompatibilityUnknown('Steam metadata is nested too deeply')
        result = {}
        while index < len(tokens):
            kind, key = tokens[index]
            if kind == '}':
                if not nested:
                    raise CompatibilityUnknown('Unexpected closing Steam metadata brace')
                index += 1
                return result
            if kind != 'text':
                raise CompatibilityUnknown('Expected a Steam metadata key')
            key = key.casefold()
            if key in result:
                raise CompatibilityUnknown('Duplicate Steam metadata key')
            index += 1
            if index >= len(tokens):
                raise CompatibilityUnknown('Steam metadata key has no value')
            kind, value = tokens[index]
            index += 1
            if kind == '{':
                value = object_value(depth + 1, True)
            elif kind != 'text':
                raise CompatibilityUnknown('Expected a Steam metadata value')
            result[key] = value
        if nested:
            raise CompatibilityUnknown('Steam metadata has an unfinished object')
        return result

    return object_value(0, False)


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_vdf(path):
    path_identity = file_identity(path.stat())
    with path.open('rb') as stream:
        before = file_identity(os.fstat(stream.fileno()))
        # Windows Python can expose different ctime semantics via stat/fstat.
        # Compare that field only within the same API; file ID/size/mtime agree.
        if before[:4] != path_identity[:4]:
            raise CompatibilityUnknown('Steam metadata changed before being read')
        raw = stream.read(MAX_VDF_BYTES + 1)
        after = file_identity(os.fstat(stream.fileno()))
    if len(raw) > MAX_VDF_BYTES:
        raise CompatibilityUnknown('Steam metadata is too large')
    if before != after or path_identity != file_identity(path.stat()):
        raise CompatibilityUnknown('Steam metadata changed while being read')
    return parse_vdf(raw.decode('utf-8-sig')), (path_identity, hashlib.sha256(raw).hexdigest())


def registry_steam_roots():
    """Only query Steam installation locations; do not enumerate other keys."""
    try:
        import winreg
    except ImportError:
        return [], ['Steam registry discovery requires Windows']
    roots, problems = [], []
    queries = ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath', 0),
               (winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', 'InstallPath', winreg.KEY_WOW64_32KEY),
               (winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', 'InstallPath', winreg.KEY_WOW64_64KEY))
    for hive, key, value, view in queries:
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                location, kind = winreg.QueryValueEx(handle, value)
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(location, str) or not location:
                raise CompatibilityUnknown('Steam registry location is invalid')
            roots.append(Path(os.path.expandvars(location)))
        except FileNotFoundError:
            continue
        except (OSError, CompatibilityUnknown) as error:
            problems.append(f'Steam registry location could not be read: {error}')
    return roots, problems


def _decimal(data, key, *, default=None):
    value = data.get(key)
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,20}', value):
        raise CompatibilityUnknown(f'Steam metadata has an invalid {key}')
    return int(value)


def _installed_state(data):
    # Conservatively accept only the observed fully installed state. Other flag
    # combinations may mean update/validation/staging; don't guess from a file.
    if _decimal(data, 'stateflags') != 4:
        raise CompatibilityUnknown('Steam reports an update, validation or incomplete installation', 'DFP-INSTALL-INCOMPLETE')
    build = _decimal(data, 'buildid')
    if not build:
        raise CompatibilityUnknown('Steam has not recorded an installed build', 'DFP-INSTALL-INCOMPLETE')
    if _decimal(data, 'targetbuildid', default=0) not in (0, build):
        raise CompatibilityUnknown('Steam has a different target build pending', 'DFP-INSTALL-INCOMPLETE')
    if _decimal(data, 'updateresult', default=0) != 0:
        raise CompatibilityUnknown('Steam reports an unsuccessful update', 'DFP-INSTALL-INCOMPLETE')
    if _decimal(data, 'scheduledautoupdate', default=0) != 0:
        raise CompatibilityUnknown('Steam has a game update scheduled', 'DFP-INSTALL-INCOMPLETE')
    for total, done in (('bytestodownload', 'bytesdownloaded'), ('bytestostage', 'bytesstaged')):
        if total in data or done in data:
            if _decimal(data, total) != _decimal(data, done):
                raise CompatibilityUnknown('Steam has unfinished download or staging work', 'DFP-INSTALL-INCOMPLETE')
    return str(build)


class GameCompatibilityChecker:
    def __init__(self, steam_roots=None, *, expected_sha256=GAME_SHA256, expected_build=GAME_BUILD,
                 process_discovery=None):
        self.steam_roots = None if steam_roots is None else tuple(map(Path, steam_roots))
        if not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
            raise ValueError('Expected executable hash must be a SHA256')
        self.expected_sha256, self.expected_build = expected_sha256.lower(), expected_build
        self.process_discovery = running_game_processes if process_discovery is None else process_discovery
        self._hashes = {}
        self._claimed_libraries = set()

    def _build_result(self, executable, digest, identity):
        match_kind = 'exact' if digest == self.expected_sha256 else None
        if match_kind is None and self.expected_sha256 == GAME_SHA256:
            # Shared with the Helper: changing a signing certificate alone may
            # preserve every executable byte/layout the mod depends on. This
            # never enables a separately compiled store binary.
            from executable_identity import identify_executable
            identified = identify_executable(executable)
            reported_identity = identified.get('file_identity', {})
            if (identified.get('sha256') != digest
                    or tuple(reported_identity.get(key) for key in
                             ('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns')) != identity):
                raise CompatibilityUnknown('Game executable changed during identity validation', 'DFP-FILE-CHANGED')
            if identified.get('compatible') and identified.get('match_kind') == 'certificate_equivalent':
                match_kind = 'certificate_equivalent'
        code = ('DFP-OK' if match_kind == 'exact' else 'DFP-EQUIVALENT-BUILD'
                if match_kind == 'certificate_equivalent' else 'DFP-UNTESTED-BUILD')
        result = {'state': 'compatible' if match_kind else 'unsupported', 'reason_code': code,
                  'reason': COMPATIBILITY_MESSAGES[code], 'match_kind': match_kind}
        pe = read_pe_metadata(executable, identity)
        if pe is not None:
            result['pe'] = pe
        return result

    def _libraries(self):
        roots, problems = registry_steam_roots() if self.steam_roots is None else (self.steam_roots, [])
        libraries = {}
        self._claimed_libraries = set()

        def add(path):
            if not path.is_absolute():
                raise CompatibilityUnknown('Steam library location is not absolute')
            path = path.resolve()
            libraries[str(path).casefold()] = path
            if len(libraries) > MAX_LIBRARIES:
                raise CompatibilityUnknown('Too many Steam library locations')
            return str(path).casefold()

        for root in roots:
            try:
                add(root)
                for relative in ('steamapps/libraryfolders.vdf', 'config/libraryfolders.vdf'):
                    try:
                        data, _ = read_vdf(root / relative)
                    except FileNotFoundError:
                        continue
                    data = data.get('libraryfolders')
                    if not isinstance(data, dict):
                        raise CompatibilityUnknown('Steam library list has no libraryfolders object')
                    for key, entry in data.items():
                        if key.isdecimal():
                            location = entry.get('path') if isinstance(entry, dict) else entry
                            if not isinstance(location, str) or not location:
                                raise CompatibilityUnknown('Steam library location is missing')
                            identifier = add(Path(location))
                            if isinstance(entry, dict) and isinstance(entry.get('apps'), dict) and APP_ID in entry['apps']:
                                self._claimed_libraries.add(identifier)
            except (OSError, ValueError) as error:
                problems.append(f'Steam library discovery is incomplete: {error}')
        return list(libraries.values())[:MAX_LIBRARIES], problems

    def _hash(self, executable, manifest_snapshot):
        identity = file_identity(executable.stat())
        if not 0 < identity[2] <= MAX_EXECUTABLE_BYTES:
            raise CompatibilityUnknown('Game executable size is outside the bounded compatibility check')
        cache_key = (identity, manifest_snapshot)
        previous = self._hashes.get(str(executable))
        if identity[1] and previous and previous[0] == cache_key:
            return previous[1], identity, True
        with executable.open('rb') as stream:
            info = os.fstat(stream.fileno())
            opened_identity = file_identity(info)
            if not stat.S_ISREG(info.st_mode) or opened_identity[:4] != identity[:4]:
                raise CompatibilityUnknown('Game executable changed before hashing', 'DFP-FILE-CHANGED')
            hasher = hashlib.sha256()
            remaining = identity[2]
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise CompatibilityUnknown('Game executable became shorter while hashing', 'DFP-FILE-CHANGED')
                hasher.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise CompatibilityUnknown('Game executable became longer while hashing', 'DFP-FILE-CHANGED')
            digest = hasher.hexdigest()
            if file_identity(os.fstat(stream.fileno())) != opened_identity:
                raise CompatibilityUnknown('Game executable changed while hashing', 'DFP-FILE-CHANGED')
        if file_identity(executable.stat()) != identity:
            raise CompatibilityUnknown('Game executable was replaced while hashing', 'DFP-FILE-CHANGED')
        return digest, identity, False

    def _installation(self, library, manifest):
        result = {'source': 'steam', 'library': str(library), 'manifest': str(manifest), 'state': 'unknown'}
        executable = None
        try:
            parsed, snapshot = read_vdf(manifest)
            data = parsed.get('appstate')
            if not isinstance(data, dict) or data.get('appid') != APP_ID:
                raise CompatibilityUnknown('Steam app manifest does not identify the expected game')
            result['state_flags'] = data.get('stateflags')
            result['steam_build'] = data.get('buildid')
            name = data.get('installdir')
            if (not isinstance(name, str) or not name or len(name) > 255 or name in ('.', '..')
                    or name != name.rstrip(' .') or any(c in name for c in '/\\:')
                    or any(ord(c) < 32 for c in name)):
                raise CompatibilityUnknown('Steam installation folder is not a safe single folder name')
            common = (library / 'steamapps/common').resolve()
            installation = (common / name).resolve()
            executable = (installation / EXE_RELATIVE).resolve()
            if (not installation.is_relative_to(common) or not executable.is_relative_to(installation)
                    or executable.name.casefold() != GAME_EXE.casefold()):
                raise CompatibilityUnknown('Game executable path leaves the declared Steam installation')
            result.update(installation=str(installation), executable=str(executable))
            result['steam_build'] = _installed_state(data)
            digest, identity, cached = self._hash(executable, snapshot)
            build = self._build_result(executable, digest, identity)
            _, final_snapshot = read_vdf(manifest)
            if final_snapshot != snapshot or file_identity(executable.stat()) != identity:
                raise CompatibilityUnknown('Steam installation changed during the compatibility check', 'DFP-FILE-CHANGED')
            self._hashes[str(executable)] = ((identity, snapshot), digest)
            result.update(sha256=digest, hash_cached=cached,
                          file_identity=dict(zip(('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'), identity)),
                          **build)
        except (OSError, ValueError) as error:
            if executable is not None:
                self._hashes.pop(str(executable), None)
            result['reason'] = f'Compatibility could not be confirmed: {error}'
            result['reason_code'] = _error_code(error, default='DFP-STEAM-METADATA')
            result['windows_error'] = getattr(error, 'winerror', None)
        return result

    def _direct_copy(self, path, source, snapshot=None):
        result = {'source': source, 'state': 'unknown'}
        executable = None
        try:
            executable = Path(path)
            if not executable.is_absolute():
                raise CompatibilityUnknown('Select an absolute game executable path', 'DFP-WRONG-EXE')
            executable = executable.resolve()
            result['executable'] = str(executable)
            if executable.name.casefold() not in recognized_executable_names():
                raise CompatibilityUnknown('Select the Dungeons shipping executable, not the launcher', 'DFP-WRONG-EXE')
            digest, identity, cached = self._hash(executable, snapshot)
            build = self._build_result(executable, digest, identity)
            if file_identity(executable.stat()) != identity:
                raise CompatibilityUnknown('Game executable changed during the compatibility check', 'DFP-FILE-CHANGED')
            self._hashes[str(executable)] = ((identity, snapshot), digest)
            result.update(sha256=digest, hash_cached=cached,
                          file_identity=dict(zip(('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'), identity)),
                          **build)
        except (OSError, TypeError, ValueError) as error:
            if executable is not None:
                self._hashes.pop(str(executable), None)
            result['reason'] = f'Compatibility could not be confirmed: {error}'
            result['reason_code'] = _error_code(error)
            if (result['reason_code'] == 'DFP-FILE-ACCESS' and executable is not None
                    and executable.name.casefold() == 'dungeons-wingdk-shipping.exe'):
                result['reason_code'] = 'DFP-GDK-START'
            result['windows_error'] = getattr(error, 'winerror', None)
        return result

    def _processes(self):
        candidates = self.process_discovery()
        if not isinstance(candidates, list) or len(candidates) > MAX_GAME_PROCESSES:
            raise CompatibilityUnknown('Game process discovery returned an invalid or ambiguous result')
        if any(not isinstance(item, dict) for item in candidates):
            raise CompatibilityUnknown('Game process discovery returned an invalid result')
        return candidates

    @staticmethod
    def _process_key(candidate):
        if candidate.get('error'):
            code = candidate.get('reason_code')
            if code not in ('DFP-PROCESS-ACCESS', 'DFP-PROCESS-IDENTITY', 'DFP-PROCESS-CHANGED'):
                code = 'DFP-PROCESS-IDENTITY'
            raise CompatibilityUnknown('The running game could not be identified: ' + str(candidate['error']), code)
        pid, created = candidate.get('pid'), candidate.get('creation_time')
        name, path = candidate.get('name'), candidate.get('executable')
        if (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF
                or type(created) is not int or not 0 < created < 2**64
                or not isinstance(name, str) or name.casefold() not in recognized_executable_names()
                or not isinstance(path, str) or not Path(path).is_absolute()):
            raise CompatibilityUnknown('The running game identity is incomplete', 'DFP-PROCESS-IDENTITY')
        executable = Path(path).resolve()
        if executable.name.casefold() != name.casefold():
            raise CompatibilityUnknown('The running game name and executable do not agree', 'DFP-PROCESS-IDENTITY')
        return pid, created, name.casefold(), str(executable).casefold()

    def _running_copy(self, candidate):
        result = {'source': 'running', 'state': 'unknown', 'name': candidate.get('name'),
                  'pid': candidate.get('pid'), 'executable': candidate.get('executable'),
                  'windows_error': candidate.get('windows_error')}
        try:
            key = self._process_key(candidate)
            if candidate['name'].casefold() == 'dungeons-wingdk-shipping.exe':
                from game_memory import GameReader, GameCompatibilityError
                try:
                    with GameReader(pid=candidate['pid']) as reader:
                        if str(reader.executable).casefold() != key[3]:
                            raise CompatibilityUnknown('Running Xbox copy changed', 'DFP-PROCESS-CHANGED')
                        result.update(reader.mapped_identity)
                        result.update(state='compatible', reason_code='DFP-GDK-BUILD',
                                      reason=COMPATIBILITY_MESSAGES['DFP-GDK-BUILD'], windows_error=None)
                except GameCompatibilityError as error:
                    if error.reason_code != 'DFP-UNTESTED-BUILD':
                        raise
                    result.update(state='unsupported', reason_code=error.reason_code,
                                  reason=COMPATIBILITY_MESSAGES[error.reason_code])
            else:
                result.update(self._direct_copy(candidate['executable'], 'running', ('running', key)))
            # A PID can exit or be reused while its on-disk image is checked.
            # Require the same sole process, image and creation time afterward.
            after = self._processes()
            if len(after) != 1 or self._process_key(after[0]) != key:
                raise CompatibilityUnknown('The running game changed during the compatibility check; check again', 'DFP-PROCESS-CHANGED')
            if result['state'] in ('compatible', 'unsupported') and 'file_identity' in result:
                executable = Path(result['executable'])
                identity = tuple(result['file_identity'][field] for field in
                                 ('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'))
                if file_identity(executable.stat()) != identity:
                    raise CompatibilityUnknown('The running game executable changed during the compatibility check', 'DFP-FILE-CHANGED')
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            result.update(state='unknown', reason=f'Compatibility could not be confirmed: {error}')
            result['reason_code'] = _error_code(error, process=True, default='DFP-PROCESS-IDENTITY')
            result['windows_error'] = getattr(error, 'winerror', None) or result.get('windows_error')
        if result['state'] == 'unknown':
            path = result.get('executable')
            if isinstance(path, str) and path:
                self._hashes.pop(str(Path(path)), None)
            # Do not advertise a digest from an incomplete process snapshot.
            for field in ('sha256', 'hash_cached', 'file_identity', 'pe', 'match_kind',
                          'mapped_code_sha256', 'unwind_sha256', 'profile'):
                result.pop(field, None)
        return result

    def _result(self, state, reason, source, copies, problems, reason_code=None):
        live_paths = {item['executable'] for item in copies if isinstance(item.get('executable'), str)}
        self._hashes = {path: value for path, value in self._hashes.items() if path in live_paths}
        if reason_code is None:
            reason_code = _reason_code(copies[0]) if len(copies) == 1 else DEFAULT_REASON_CODES.get(state, 'DFP-CHECK-INCOMPLETE')
        return {'state': state, 'reason': reason, 'source': source,
                'reason_code': reason_code,
                'windows_error': copies[0].get('windows_error') if len(copies) == 1 else None,
                'app_id': APP_ID, 'expected_build': self.expected_build,
                'expected_sha256': self.expected_sha256, 'copies': copies, 'discovery_problems': problems,
                'checked_utc': datetime.now(timezone.utc).isoformat(),
                'authoritative_check': 'The helper independently verifies its actual running game executable before writes'}

    def check(self, selected_executable=None):
        """Check the running game, a selected EXE, then installed Steam copies.

        Launchers do not determine compatibility: the pinned executable
        identity does. Steam discovery remains a convenience when no game
        is running and no executable was selected. An unreadable or ambiguous
        running game must never be replaced by an unrelated installed copy.
        """
        try:
            candidates = self._processes()
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            result = self._result('unknown', 'The running game could not be checked; try again',
                                  'running', [], [str(error)],
                                  _error_code(error, process=True, default='DFP-DISCOVERY-FAILED'))
            result['windows_error'] = getattr(error, 'winerror', None)
            return result
        if len(candidates) > 1:
            return self._result('unknown', 'Close duplicate copies of the game before checking compatibility',
                                'running', [], ['More than one recognized game process is running'], 'DFP-MULTIPLE-GAMES')
        if candidates:
            copy = self._running_copy(candidates[0])
            return self._result(copy['state'], copy['reason'], 'running', [copy], [])
        if selected_executable is not None:
            copy = self._direct_copy(selected_executable, 'selected')
            return self._result(copy['state'], copy['reason'], 'selected', [copy], [])
        libraries, problems = self._libraries()
        copies = []
        for library in libraries:
            manifest = library / 'steamapps' / f'appmanifest_{APP_ID}.acf'
            try:
                manifest.stat()
            except FileNotFoundError:
                if str(library).casefold() in self._claimed_libraries:
                    problems.append(f'Steam lists this game in {library}, but its app manifest is unavailable')
                continue
            except OSError as error:
                problems.append(f'Steam app manifest could not be inspected: {error}')
                continue
            copies.append(self._installation(library, manifest))
        states = {copy['state'] for copy in copies}
        code = None
        if problems or 'unknown' in states or len(states) > 1:
            state, reason = 'unknown', 'Installation checks are incomplete or installed copies disagree; see details'
            code = ('DFP-MIXED-INSTALLS' if len(states) > 1 else 'DFP-STEAM-METADATA' if problems
                    else _reason_code(copies[0]) if len(copies) == 1 else 'DFP-CHECK-INCOMPLETE')
        elif not copies:
            state, reason = 'not_found', 'Launch the game from your launcher, or choose its shipping executable to check compatibility'
        elif states == {'compatible'}:
            state, reason = 'compatible', 'All discovered installed copies match the supported executable'
            code = ('DFP-EQUIVALENT-BUILD' if any(copy.get('match_kind') == 'certificate_equivalent' for copy in copies)
                    else 'DFP-OK')
        else:
            state, reason = 'unsupported', 'The installed game differs from this mod\'s supported executable; a tested mod update is required'
        return self._result(state, reason, 'steam' if copies or problems else 'none', copies, problems, code)
