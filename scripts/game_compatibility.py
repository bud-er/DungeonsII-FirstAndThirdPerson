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
import subprocess

from build_info import APP_VERSION, GAME_BUILD, GAME_EXE, GAME_SHA256


APP_ID = '1912410'
MAX_VDF_BYTES = 1024 * 1024
MAX_LIBRARIES = 64
MAX_GAME_PROCESSES = 16
MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
EXE_RELATIVE = Path('Dungeons') / 'Binaries' / 'Win64' / GAME_EXE


class CompatibilityUnknown(ValueError):
    pass


def running_game_processes():
    # Import lazily so parser/file tests and the launcher can load this module
    # without touching Windows process APIs. Discovery uses query-only access.
    from game_memory import discover_game_processes
    return discover_game_processes()


def recognized_executable_names():
    from game_memory import GAME_PROCESS_NAMES
    return {name.casefold() for name in GAME_PROCESS_NAMES}


def compatibility_report(result):
    """Return shareable build facts without local paths, process IDs or errors.

    The UI explicitly saves this report when the player asks. No files are
    written and no network requests are made here. Executable hashes identify
    builds for investigation; they never add a build to the supported list.
    """
    valid_states = {'compatible', 'unsupported', 'unknown', 'not_found'}
    valid_sources = {'running', 'selected', 'steam', 'none'}
    state = result.get('state') if result.get('state') in valid_states else 'unknown'
    source = result.get('source') if result.get('source') in valid_sources else 'none'
    checked = result.get('checked_utc', '')
    try:
        checked = datetime.fromisoformat(checked).isoformat()
    except (TypeError, ValueError):
        checked = None
    report = {'schema': 1, 'mod_version': APP_VERSION, 'state': state, 'source': source,
              'checked_utc': checked, 'supported_reference_build': GAME_BUILD,
              'supported_sha256': GAME_SHA256, 'copies': [],
              'note': 'Local build identification only; this report does not change compatibility or upload anything.'}
    names = recognized_executable_names()
    for item in result.get('copies', [])[:MAX_LIBRARIES]:
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
                 'state': item.get('state') if item.get('state') in valid_states else 'unknown'}
        digest = item.get('sha256')
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            entry['sha256'] = digest.lower()
        size = item.get('file_identity', {}).get('size')
        if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_EXECUTABLE_BYTES:
            entry['size'] = size
        steam_build = item.get('steam_build')
        if source == 'steam' and isinstance(steam_build, str) and re.fullmatch(r'\d{1,20}', steam_build):
            entry['steam_build'] = steam_build
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
        raise CompatibilityUnknown('Steam reports an update, validation or incomplete installation')
    build = _decimal(data, 'buildid')
    if not build:
        raise CompatibilityUnknown('Steam has not recorded an installed build')
    if _decimal(data, 'targetbuildid', default=0) not in (0, build):
        raise CompatibilityUnknown('Steam has a different target build pending')
    if _decimal(data, 'updateresult', default=0) != 0:
        raise CompatibilityUnknown('Steam reports an unsuccessful update')
    if _decimal(data, 'scheduledautoupdate', default=0) != 0:
        raise CompatibilityUnknown('Steam has a game update scheduled')
    for total, done in (('bytestodownload', 'bytesdownloaded'), ('bytestostage', 'bytesstaged')):
        if total in data or done in data:
            if _decimal(data, total) != _decimal(data, done):
                raise CompatibilityUnknown('Steam has unfinished download or staging work')
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
                raise CompatibilityUnknown('Game executable changed before hashing')
            hasher = hashlib.sha256()
            remaining = identity[2]
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise CompatibilityUnknown('Game executable became shorter while hashing')
                hasher.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise CompatibilityUnknown('Game executable became longer while hashing')
            digest = hasher.hexdigest()
            if file_identity(os.fstat(stream.fileno())) != opened_identity:
                raise CompatibilityUnknown('Game executable changed while hashing')
        if file_identity(executable.stat()) != identity:
            raise CompatibilityUnknown('Game executable was replaced while hashing')
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
            _, final_snapshot = read_vdf(manifest)
            if final_snapshot != snapshot or file_identity(executable.stat()) != identity:
                raise CompatibilityUnknown('Steam installation changed during the compatibility check')
            self._hashes[str(executable)] = ((identity, snapshot), digest)
            result.update(sha256=digest, hash_cached=cached,
                          file_identity=dict(zip(('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'), identity)),
                          state='compatible' if digest == self.expected_sha256 else 'unsupported',
                          reason='Executable matches the supported SHA256' if digest == self.expected_sha256
                          else 'Installed executable differs from this mod\'s supported SHA256; a tested mod update is required')
        except (OSError, ValueError) as error:
            if executable is not None:
                self._hashes.pop(str(executable), None)
            result['reason'] = f'Compatibility could not be confirmed: {error}'
        return result

    def _direct_copy(self, path, source, snapshot=None):
        result = {'source': source, 'state': 'unknown'}
        executable = None
        try:
            executable = Path(path)
            if not executable.is_absolute():
                raise CompatibilityUnknown('Select an absolute game executable path')
            executable = executable.resolve()
            result['executable'] = str(executable)
            if executable.name.casefold() not in recognized_executable_names():
                raise CompatibilityUnknown('Select the Dungeons shipping executable, not the launcher')
            digest, identity, cached = self._hash(executable, snapshot)
            if file_identity(executable.stat()) != identity:
                raise CompatibilityUnknown('Game executable changed during the compatibility check')
            self._hashes[str(executable)] = ((identity, snapshot), digest)
            result.update(sha256=digest, hash_cached=cached,
                          file_identity=dict(zip(('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'), identity)),
                          state='compatible' if digest == self.expected_sha256 else 'unsupported',
                          reason='Executable matches the supported SHA256' if digest == self.expected_sha256
                          else 'This executable differs from the tested build; a tested mod update is required')
        except (OSError, TypeError, ValueError) as error:
            if executable is not None:
                self._hashes.pop(str(executable), None)
            result['reason'] = f'Compatibility could not be confirmed: {error}'
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
            raise CompatibilityUnknown('The running game could not be identified: ' + str(candidate['error']))
        pid, created = candidate.get('pid'), candidate.get('creation_time')
        name, path = candidate.get('name'), candidate.get('executable')
        if (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF
                or type(created) is not int or not 0 < created < 2**64
                or not isinstance(name, str) or name.casefold() not in recognized_executable_names()
                or not isinstance(path, str) or not Path(path).is_absolute()):
            raise CompatibilityUnknown('The running game identity is incomplete')
        executable = Path(path).resolve()
        if executable.name.casefold() != name.casefold():
            raise CompatibilityUnknown('The running game name and executable do not agree')
        return pid, created, name.casefold(), str(executable).casefold()

    def _running_copy(self, candidate):
        result = {'source': 'running', 'state': 'unknown', 'name': candidate.get('name'),
                  'pid': candidate.get('pid'), 'executable': candidate.get('executable')}
        try:
            key = self._process_key(candidate)
            result.update(self._direct_copy(candidate['executable'], 'running', ('running', key)))
            # A PID can exit or be reused while its on-disk image is checked.
            # Require the same sole process, image and creation time afterward.
            after = self._processes()
            if len(after) != 1 or self._process_key(after[0]) != key:
                raise CompatibilityUnknown('The running game changed during the compatibility check; check again')
            if result['state'] in ('compatible', 'unsupported'):
                executable = Path(result['executable'])
                identity = tuple(result['file_identity'][field] for field in
                                 ('device', 'file_id', 'size', 'mtime_ns', 'ctime_ns'))
                if file_identity(executable.stat()) != identity:
                    raise CompatibilityUnknown('The running game executable changed during the compatibility check')
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            result.update(state='unknown', reason=f'Compatibility could not be confirmed: {error}')
        if result['state'] == 'unknown':
            path = result.get('executable')
            if isinstance(path, str) and path:
                self._hashes.pop(str(Path(path)), None)
            # Do not advertise a digest from an incomplete process snapshot.
            for field in ('sha256', 'hash_cached', 'file_identity'):
                result.pop(field, None)
        return result

    def _result(self, state, reason, source, copies, problems):
        live_paths = {item['executable'] for item in copies if isinstance(item.get('executable'), str)}
        self._hashes = {path: value for path, value in self._hashes.items() if path in live_paths}
        return {'state': state, 'reason': reason, 'source': source,
                'app_id': APP_ID, 'expected_build': self.expected_build,
                'expected_sha256': self.expected_sha256, 'copies': copies, 'discovery_problems': problems,
                'checked_utc': datetime.now(timezone.utc).isoformat(),
                'authoritative_check': 'The helper independently verifies its actual running game executable before writes'}

    def check(self, selected_executable=None):
        """Check the running game, a selected EXE, then installed Steam copies.

        Launchers do not determine compatibility: only the exact executable
        fingerprint does. Steam discovery remains a convenience when no game
        is running and no executable was selected. An unreadable or ambiguous
        running game must never be replaced by an unrelated installed copy.
        """
        try:
            candidates = self._processes()
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            return self._result('unknown', 'The running game could not be checked; try again',
                                'running', [], [str(error)])
        if len(candidates) > 1:
            return self._result('unknown', 'Close duplicate copies of the game before checking compatibility',
                                'running', [], ['More than one recognized game process is running'])
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
        if problems or 'unknown' in states or len(states) > 1:
            state, reason = 'unknown', 'Installation checks are incomplete or installed copies disagree; see details'
        elif not copies:
            state, reason = 'not_found', 'Launch the game from your launcher, or choose its shipping executable to check compatibility'
        elif states == {'compatible'}:
            state, reason = 'compatible', 'All discovered installed copies match the supported executable'
        else:
            state, reason = 'unsupported', 'The installed game differs from this mod\'s supported executable; a tested mod update is required'
        return self._result(state, reason, 'steam' if copies or problems else 'none', copies, problems)
