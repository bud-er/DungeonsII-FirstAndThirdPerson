"""Validated, per-user startup preferences; no game configuration is edited."""
import json
import math
import os
from pathlib import Path
from build_info import NATIVE_ROLL_AVAILABLE

DEFAULTS = dict(fov=115, height=55, sensitivity=.12, hide_body=True,
                damage_flash=True, hide_silhouettes=True, first_person_controls=True,
                steer_roll=True, coop_diagnostics=True, native_roll_hook=False)
RANGES = dict(fov=(65,115), height=(10,120), sensitivity=(.01,2))


def data_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'DungeonsFirstPerson'


def validate_settings(values):
    result = {}
    for key, default in DEFAULTS.items():
        value = values.get(key, default)
        if key in RANGES:
            if isinstance(value, bool):
                raise ValueError(f'{key} must be a number.')
            value = float(value)
            low, high = RANGES[key]
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{key} must be between {low:g} and {high:g}.')
        elif not isinstance(value, bool):
            raise ValueError(f'{key} must be enabled or disabled.')
        result[key] = value
    return result


def load_settings(path):
    if not path.exists():
        return DEFAULTS.copy()
    values = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(values, dict):
        raise ValueError('Settings must contain a JSON object.')
    result = validate_settings(values)
    if not NATIVE_ROLL_AVAILABLE:
        result['native_roll_hook'] = False
    return result


def save_settings(path, values):
    values = validate_settings(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(values, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def helper_arguments(values):
    values = validate_settings(values)
    if values['native_roll_hook'] and not NATIVE_ROLL_AVAILABLE:
        raise ValueError('Native roll experiment disabled after reversed co-op travel and a game exit.')
    if values['native_roll_hook'] and (not values['first_person_controls'] or not values['steer_roll']):
        raise ValueError('The native roll prototype requires first-person controls and dodge steering enabled.')
    args = ['--interactive', '--fov', str(values['fov']), '--height', str(values['height']),
            '--sensitivity', str(values['sensitivity'])]
    for key, flag in (('hide_body','--no-avatar'), ('damage_flash','--no-damage-flash'),
                      ('hide_silhouettes','--keep-mob-silhouettes'),
                      ('steer_roll','--native-roll-direction')):
        if not values[key]:
            args.append(flag)
    if not values['first_person_controls']:
        args.extend(['--native-combat', '--no-wasd', '--native-avatar-facing'])
    if values['coop_diagnostics']:
        args.append('--coop-diagnostics')
    if values['native_roll_hook']:
        args.append('--native-roll-hook')
    return args
