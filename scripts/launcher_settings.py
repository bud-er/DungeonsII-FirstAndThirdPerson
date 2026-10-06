"""Validated, per-user startup preferences; no game configuration is edited."""
import json
import math
import os
from pathlib import Path
from build_info import NATIVE_ROLL_AVAILABLE

DEFAULTS = dict(fov=115, height=55, sensitivity=.12, hide_body=True,
                damage_flash=True, hide_silhouettes=True, first_person_controls=True,
                steer_roll=True, coop_diagnostics=True, native_roll_hook=False,
                third_person=True, shoulder_switch=True, shoulder_distance=220., shoulder_side=45.,
                shoulder_height=65., shoulder_fov=90., transition_duration=.45,
                sprint=False, sprint_multiplier=1.2, direction_sprint=True,
                quick_turn=True, keyboard_gestures=True,
                hide_armor_helmet=False, hide_armor_chest=False,
                hide_armor_pants=False, hide_armor_boots=False, inventory_armor_toggle=True)
RANGES = dict(fov=(65,115), height=(10,120), sensitivity=(.01,2),
              shoulder_distance=(80,400), shoulder_side=(-120,120),
              shoulder_height=(10,160), shoulder_fov=(65,115),
              transition_duration=(.15,1.2), sprint_multiplier=(1.05,1.35))


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
    if values['sprint'] and not values['first_person_controls']:
        raise ValueError('Sprint requires camera-relative movement controls. Enable them or turn sprint off.')
    args = ['--interactive', '--fov', str(values['fov']), '--height', str(values['height']),
            '--sensitivity', str(values['sensitivity'])]
    for slot in ('helmet', 'chest', 'pants', 'boots'):
        if values['hide_armor_'+slot]:
            args.append('--hide-armor-'+slot)
    if not values['inventory_armor_toggle']:
        args.append('--no-inventory-armor-toggle')
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
    for key in ('shoulder_distance', 'shoulder_side', 'shoulder_height', 'shoulder_fov',
                'transition_duration', 'sprint_multiplier'):
        args.extend(['--'+key.replace('_', '-'), str(values[key])])
    if values['sprint']:
        args.append('--sprint')
    for key, flag in (('third_person', '--no-third-person'), ('shoulder_switch', '--no-shoulder-switch'),
                      ('quick_turn', '--no-quick-turn'),
                      ('direction_sprint', '--no-direction-sprint'),
                      ('keyboard_gestures', '--no-keyboard-gestures')):
        if not values[key]:
            args.append(flag)
    return args
