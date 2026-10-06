"""First-person camera with an explicitly opt-in native roll prototype."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import math
import struct
import sys
import threading
import time

from game_memory import GameReader, find_game_pid, memory_error
from inspect_camera import Reflection, OBJECT_ARRAY_RVA
from camera_transition import smoothstep
from camera_pose import CameraPose
from menu_state import MenuState
from controller_hold import CameraHold
from quick_turn import QuickTurn
from camera_discovery import CameraDiscovery
from camera_recovery import CameraRecovery, failure_path
from local_player_scope import LocalPlayerScope


class CameraMemory(GameReader):
    ACCESS = 0x0438  # QUERY_INFORMATION | VM_READ | VM_WRITE | VM_OPERATION; no remote threads.

    def write(self, address, data):
        fn = self.kernel.WriteProcessMemory
        fn.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        fn.restype = W.BOOL
        buffer, count = C.create_string_buffer(data), C.c_size_t()
        if not fn(self.handle, address, buffer, len(data), C.byref(count)) or count.value != len(data):
            raise memory_error('write', address, len(data), count.value)


class CameraSession:
    def __init__(self, game, height=55, fov=95, previous=None):
        # Menus retain the same player: reuse discovery, but take fresh camera,
        # avatar and equipment baselines after restoring the previous session.
        if previous is not None:
            if previous.game is not game:
                raise RuntimeError('Cannot reuse discovery across game processes')
            for obj in (previous.pc, previous.pawn, previous.arm, previous.camera, previous.root):
                if previous.identity(obj) != previous.identities[obj]:
                    raise RuntimeError('Player changed while the camera was paused')
            if previous.pointer(previous.pc, 'Controller', 'Pawn') != previous.pawn:
                raise RuntimeError('Controlled pawn changed while paused')
        self.game, self.reflection = game, previous.reflection if previous else Reflection(game)
        self.originals, self.identities, self.plan = {}, {}, []
        self.identity_kinds = {}
        self.height, self.fov = height, fov
        self.ancestors = previous.ancestors.copy() if previous else {}
        objects = self.objects = previous.objects if previous else list(self.reflection.objects())
        native = previous.native.copy() if previous else {}
        if previous is None:
            for obj in objects:
                if self.reflection.header(obj['class'])['name'] in ('Class', 'ScriptStruct'):
                    native[obj['name']] = obj['address']
        needed = ('Vector', 'Rotator', 'Actor', 'Controller', 'PlayerController', 'PlayerCharacter',
                  'GameplayPlayerController', 'SceneComponent', 'SpringArmComponent',
                  'LovikaSpringArmComponent', 'CameraComponent')
        self.layouts = previous.layouts.copy() if previous else {}
        self.layouts.update({name: self.reflection.properties(native[name]) for name in needed})
        self.native = native
        for name in ('Vector', 'Rotator'):
            if [p['offset'] for p in self.layouts[name]['properties']] != [0, 8, 16]:
                raise RuntimeError(f'{name} layout validation failed')
        candidates = [previous.pc] if previous else []
        for obj in (() if previous else objects):
            if obj['flags'] & 0x30 or not self.is_a(obj['class'], 'GameplayPlayerController'):
                continue
            player = self.pointer(obj['address'], 'PlayerController', 'Player')
            if player and self.is_a(self.reflection.header(player)['class'], 'LocalPlayer'):
                candidates.append(obj['address'])
        if len(candidates) != 1:
            raise RuntimeError(f'Expected one local gameplay controller, found {len(candidates)}. Enter a playable area first.')
        self.pc = candidates[0]
        self.pawn = self.pointer(self.pc, 'Controller', 'Pawn')
        self.arm = self.pointer(self.pawn, 'PlayerCharacter', 'CameraSpringArm')
        self.camera = self.pointer(self.pawn, 'PlayerCharacter', 'PlayerCamera')
        if self.pointer(self.pawn, 'PlayerCharacter', 'CurrentCamera') != self.camera:
            raise RuntimeError('The normal player camera is not active; ending test')
        self.root = self.pointer(self.pawn, 'Actor', 'RootComponent')
        for address, kind in ((self.pc, 'GameplayPlayerController'), (self.pawn, 'PlayerCharacter'),
                              (self.arm, 'SpringArmComponent'), (self.camera, 'CameraComponent'), (self.root, 'SceneComponent')):
            self.remember(address, kind)
        self.local_scope = LocalPlayerScope(self)
        rotation = self.value(self.root, 'SceneComponent', 'RelativeRotation', '<ddd')
        self.yaw, self.pitch = rotation[1], 0.0
        if not all(math.isfinite(x) and abs(x) < 1e7 for x in rotation):
            raise RuntimeError('Player rotation is not plausible')
        self.pose = CameraPose(self)
        self.controller_profile = None
        self.combat_profile = None
        self.native_roll_profile = None
        self.roll_target_profile = None
        self.keyboard_movement = None
        self.mouse_bindings = None
        self.cursor_profile = None
        self.cursor_fields = set()
        self.input_paused = False
        self.avatar_profile = None
        self.damage_feedback = None
        self.silhouette_profile = None
        self.blend, self.blend_target, self.blend_started = 1., 1., None
        from local_view_target import LocalViewTarget
        self.view_target_profile = LocalViewTarget(self)
        self.dialogue_active = False

    def ensure_layout(self, name):
        if name not in self.layouts:
            self.layouts[name] = self.reflection.properties(self.native[name])
        return self.layouts[name]

    def enable_controller_profile(self):
        from controller_profile import ControllerProfile
        self.controller_profile = ControllerProfile(self)

    def enable_combat_profile(self, *, steer_roll=True, filter_ranged=False):
        from combat_profile import CombatProfile
        self.combat_profile = CombatProfile(self, steer_roll=steer_roll, filter_ranged=filter_ranged)

    def enable_native_roll_profile(self):
        from native_roll_hook import NativeRollProfile
        # Pawn is not needed by camera discovery, but ownership reflection is
        # required by the native gate before any executable-memory write.
        self.ensure_layout('Pawn')
        self.native_roll_profile = NativeRollProfile(self)

    def enable_roll_target_profile(self):
        from roll_target_data import RollTargetDataProfile
        self.ensure_layout('Pawn')
        self.roll_target_profile = RollTargetDataProfile(self)

    def enable_keyboard_movement(self):
        from keyboard_movement import KeyboardMovement
        self.keyboard_movement = KeyboardMovement(self)

    def enable_mouse_bindings(self):
        from mouse_bindings import MouseBindings
        self.mouse_bindings = MouseBindings(self)

    def hide_cursor(self):
        from cursor_profile import CursorProfile
        self.cursor_profile = CursorProfile(self)
        for name in ('DefaultMouseCursor', 'CurrentMouseCursor'):
            prop = self.field(self.pc, 'PlayerController', name)
            if prop['type'] != 'ByteProperty' or prop['size'] != 1:
                raise RuntimeError('Unsupported native mouse cursor layout')
        start = len(self.plan)
        self.add_bool(self.pc, 'PlayerController', 'bShowMouseCursor', False)
        for name in ('DefaultMouseCursor', 'CurrentMouseCursor'):
            self.add(self.pc, 'PlayerController', name, '<B', 0)  # EMouseCursor::None.
        self.cursor_fields = {address for address, _, _ in self.plan[start:]}

    def set_ui_input(self, captured):
        """Hand input back to a small UI without detaching the local camera."""
        captured = bool(captured)
        if captured == self.input_paused:
            return
        if captured:
            for name in ('roll_target_profile', 'native_roll_profile', 'controller_profile', 'keyboard_movement',
                         'mouse_bindings', 'combat_profile', 'cursor_profile'):
                profile = getattr(self, name, None)
                if profile is not None:
                    profile.restore()
                    if profile.restore_failures:
                        raise RuntimeError(f'{name} restoration failed during UI input handoff')
            forced_cursor = {address: value for address, value, _ in self.plan if address in self.cursor_fields}
            for address in self.cursor_fields:
                obj, original, mask = self.originals[address]
                if self.identity(obj) != self.identities[obj]:
                    raise RuntimeError('Cursor controller changed during UI input handoff')
                current = self.game.read(address, len(original))
                if mask is not None:
                    if current[0] & mask != forced_cursor[address] & mask:
                        continue  # The native menu already selected its cursor.
                    original = bytes([(current[0] & ~mask) | (original[0] & mask)])
                elif current != forced_cursor[address]:
                    continue
                self.game.write(address, original)
                if self.game.read(address, len(original)) != original:
                    raise RuntimeError('Cursor field restoration failed during UI input handoff')
        else:
            # Closing a UI can replace the runtime input array. Collect fresh
            # rows before any camera-relative movement/aim resumes.
            if self.controller_profile is not None:
                self.controller_profile.last_update = 0.
            for name in ('keyboard_movement', 'mouse_bindings'):
                profile = getattr(self, name, None)
                if profile is not None:
                    profile.next_collect = 0.
        self.input_paused = captured
        print('Menu input active; first-person view retained.' if captured else
              'Gameplay input resumed in first person.', flush=True)

    def enable_avatar_profile(self, *, follow_view=True):
        from avatar_profile import AvatarProfile
        self.avatar_profile = AvatarProfile(self, follow_view=follow_view)

    def enable_silhouette_profile(self, scene):
        from silhouette_profile import SilhouetteProfile
        self.silhouette_profile = SilhouetteProfile(self, scene)

    def set_silhouette_hiding(self, scene, enabled):
        if enabled:
            if self.silhouette_profile is None:
                self.enable_silhouette_profile(scene)
        elif self.silhouette_profile is not None:
            self.silhouette_profile.restore()
            if self.silhouette_profile.restore_failures:
                raise RuntimeError('Mob silhouette restoration failed')
            # Recapture native flags if hiding is enabled again later.
            self.silhouette_profile = None

    def enable_damage_feedback(self):
        from damage_feedback import DamageFeedback
        self.damage_feedback = DamageFeedback(self)

    def is_a(self, cls, name):
        if cls not in self.ancestors:
            chain, cursor = set(), cls
            for _ in range(64):
                if not cursor:
                    break
                chain.add(self.reflection.header(cursor)['name'])
                cursor = self.game.u64(cursor + 0x40)
            self.ancestors[cls] = chain
        return name in self.ancestors[cls]

    def field(self, obj, cls, name):
        if not obj or not self.is_a(self.reflection.header(obj)['class'], cls):
            raise RuntimeError(f'Object is not a {cls}')
        prop = next(p for p in self.layouts[cls]['properties'] if p['name'] == name)
        if prop['dimensions'] != 1 or prop['offset'] + prop['size'] > self.layouts[cls]['size']:
            raise RuntimeError(f'Invalid field extent: {cls}.{name}')
        return dict(prop, address=obj + prop['offset'])

    def pointer(self, obj, cls, name):
        prop = self.field(obj, cls, name)
        if prop['type'] != 'ObjectProperty' or prop['size'] != 8:
            raise RuntimeError('Expected object pointer property')
        return self.game.u64(prop['address'])

    def value(self, obj, cls, name, fmt):
        prop = self.field(obj, cls, name)
        if struct.calcsize(fmt) != prop['size']:
            raise RuntimeError('Unexpected field size')
        return struct.unpack(fmt, self.game.read(prop['address'], prop['size']))

    def identity(self, address):
        header = self.game.read(address, 0x28)
        index = struct.unpack_from('<I', header, 0xC)[0]
        array = self.game.base + OBJECT_ARRAY_RVA
        if index >= self.game.u32(array + 0x24):
            raise RuntimeError('Object no longer in object array')
        chunks = self.game.u64(array + 0x10)
        chunk = self.game.u64(chunks + (index // 65536) * 8)
        item = self.game.read(chunk + (index % 65536) * 24, 24)
        if struct.unpack_from('<Q', item)[0] != address:
            raise RuntimeError('Object array entry changed')
        return header[0xC:0x20], item[0x10:0x14]

    def remember(self, address, cls):
        if not address or not self.is_a(self.reflection.header(address)['class'], cls):
            raise RuntimeError(f'Expected a valid {cls}')
        self.identities[address] = self.identity(address)
        self.identity_kinds[address] = cls

    def object_retired(self, address, expected):
        """Prove retirement from the saved UObject slot, without reading freed RAM.

        A failed read is not retirement. Callers must report it and retain their
        restoration record rather than declaring an unreadable live field clean.
        """
        index = struct.unpack_from('<I', expected[0])[0]
        array = self.game.base + OBJECT_ARRAY_RVA
        descriptor = self.game.read(array,0x30)
        chunks = struct.unpack_from('<Q',descriptor,0x10)[0]
        maximum,count,max_chunks,num_chunks = struct.unpack_from('<IIII',descriptor,0x20)
        if not chunks or not 0<count<=maximum<10_000_000 or not 0<num_chunks<=max_chunks<1000:
            raise RuntimeError('Invalid object array during retirement check')
        if index >= count:
            return True
        chunk = self.game.u64(chunks+(index//65536)*8)
        if not chunk:
            raise RuntimeError('Object chunk unavailable during retirement check')
        item = self.game.read(chunk+(index%65536)*24,24)
        current = self.game.read(array,0x30)
        if current[0x10:0x18] != descriptor[0x10:0x18] or self.game.u64(chunks+(index//65536)*8) != chunk:
            raise RuntimeError('Object array changed during retirement check')
        return struct.unpack_from('<Q',item)[0] != address or item[0x10:0x14] != expected[1]

    def save(self, obj, address, size, mask=None):
        if address not in self.originals:
            self.originals[address] = [obj, self.game.read(address, size), mask]
        elif mask is not None:
            self.originals[address][2] |= mask

    def add(self, obj, cls, name, fmt, *values):
        prop = self.field(obj, cls, name)
        data = struct.pack(fmt, *values)
        if len(data) != prop['size']:
            raise RuntimeError('Unexpected field size')
        self.save(obj, prop['address'], len(data))
        self.plan.append((prop['address'], data, None))

    def add_bool(self, obj, cls, name, enabled):
        prop = self.field(obj, cls, name)
        bits = prop['bool']
        if bits['fieldSize'] != 1 or bits['byteOffset'] != 0 or not bits['byteMask']:
            raise RuntimeError('Unexpected boolean storage')
        mask = bits['byteMask']
        self.save(obj, prop['address'], 1, mask)
        self.plan.append((prop['address'], mask if enabled else 0, mask))

    def tune(self, fov, height):
        self.fov = max(65, min(115, fov))
        self.height = max(10, min(120, height))

    def transition_to(self, target, start=None):
        if start is not None:
            self.blend = start
        self.blend_from, self.blend_target = self.blend, target
        self.blend_started = time.monotonic()
        self.blend_duration = max(.001, .32*abs(target-self.blend))

    def animate_out(self, has_focus):
        self.transition_to(0.)
        while self.blend > 0 and has_focus():
            started = time.perf_counter()
            self.update()
            time.sleep(max(0., 1/120-(time.perf_counter()-started)))

    def update(self):
        scope = getattr(self, 'local_scope', None)
        if scope is not None:
            scope.validate()  # No gameplay/profile writes after a possession change.
        if self.blend_started is not None:
            t = (time.monotonic()-self.blend_started)/self.blend_duration
            self.blend = self.blend_from+(self.blend_target-self.blend_from)*smoothstep(t)
            if t >= 1:
                self.blend, self.blend_started = self.blend_target, None
        for obj, expected in self.identities.items():
            if self.identity(obj) != expected:
                kind = getattr(self,'identity_kinds',{}).get(obj,'player/component')
                raise RuntimeError(f'Camera/player object changed: {kind}')
        if self.pointer(self.pc, 'Controller', 'Pawn') != self.pawn:
            raise RuntimeError('Controlled pawn changed; ending test')
        if self.pointer(self.pawn, 'PlayerCharacter', 'CurrentCamera') != self.camera:
            raise RuntimeError('The game changed cameras; ending test')
        # Apply the sampled look before slower binding/equipment/scene work.
        # Parent propagation still owns the moving camera's world position.
        self.pose.update(self.blend)
        view_target = getattr(self, 'view_target_profile', None)
        if view_target is not None:
            view_target.update(self.dialogue_active)
        input_ready = not getattr(self, 'input_paused', False)
        if input_ready and self.controller_profile is not None:
            input_ready = self.controller_profile.update() is not False
        roll_target = getattr(self, 'roll_target_profile', None)
        if roll_target is not None:
            if input_ready and self.blend >= .8 and self.blend_target == 1.:
                roll_target.update()
            elif roll_target.applied:
                roll_target.restore()
                if roll_target.restore_failures:
                    raise RuntimeError('Roll target-data gate restoration failed during transition')
        native_roll = getattr(self, 'native_roll_profile', None)
        if input_ready and native_roll is not None:
            if self.blend >= .8 and self.blend_target == 1.:
                native_roll.update()
            elif native_roll.applied:
                native_roll.restore()
                if native_roll.restore_failures:
                    raise RuntimeError('Native roll gate restoration failed during transition')
        keyboard = getattr(self, 'keyboard_movement', None)
        if input_ready and keyboard is not None:
            if self.blend >= .8:
                input_ready = keyboard.update() is not False
            elif keyboard.applied:
                keyboard.restore()
                if keyboard.restore_failures:
                    raise RuntimeError('Keyboard movement restoration failed during transition')
        mouse = getattr(self, 'mouse_bindings', None)
        if input_ready and mouse is not None:
            if self.blend >= .8:
                input_ready = mouse.update() is not False
            elif mouse.applied:
                mouse.restore()
                if mouse.restore_failures:
                    raise RuntimeError('Mouse action restoration failed during transition')
        cursor = getattr(self, 'cursor_profile', None)
        if input_ready and cursor is not None:
            if self.blend >= .8:
                cursor.update()
            elif cursor.applied:
                cursor.restore()
                if cursor.restore_failures:
                    raise RuntimeError('Cursor restoration failed during transition')
        combat = getattr(self, 'combat_profile', None)
        if input_ready and combat is not None:
            if self.blend >= .8:
                combat.update()
            elif combat.applied:
                combat.restore()
                if combat.restore_failures:
                    raise RuntimeError('Combat aim restoration failed during transition')
        if self.avatar_profile is not None:
            if self.blend >= .8:
                self.avatar_profile.update()
            elif self.avatar_profile.applied:
                self.avatar_profile.restore()
                if self.avatar_profile.restore_failures:
                    raise RuntimeError('Avatar restoration failed during camera transition')
        silhouettes = getattr(self, 'silhouette_profile', None)
        if silhouettes is not None:
            if self.blend >= .8:
                silhouettes.update()
            elif silhouettes.applied:
                silhouettes.restore()
                if silhouettes.restore_failures:
                    raise RuntimeError('Mob silhouette restoration failed')
        for address, value, mask in self.plan:
            if getattr(self, 'input_paused', False) and address in self.cursor_fields:
                continue  # Let the native UI manage cursor visibility/type.
            if mask is not None:
                value = bytes([(self.game.read(address, 1)[0] & ~mask) | value])
            self.game.write(address, value)

    def restore(self):
        # Disable and restore native decision sites before restoring normal
        # camera/input behavior. This also covers partial profile activation.
        native_manager = vars(self.game).get('native_roll_hook')
        native_ok = True
        target_manager = vars(self.game).get('roll_target_data_hook')
        if target_manager is not None:
            target_manager.restore()
            native_ok = not target_manager.restore_failures
            if native_ok and getattr(self, 'roll_target_profile', None) is not None:
                self.roll_target_profile.applied = False
        if native_manager is not None:
            native_manager.restore()
            native_ok = native_ok and not native_manager.restore_failures
            native_profile = getattr(self, 'native_roll_profile', None)
            if native_profile is not None and native_ok:
                native_profile.applied = False
        pose = getattr(self, 'pose', None)
        attachment_ok = True
        if pose is not None:
            try:
                pose.restore_attachment()
            except (OSError, RuntimeError, ValueError) as error:
                attachment_ok = False
                print(f'Camera attachment restoration failed: {error}', flush=True)
        damage = getattr(self, 'damage_feedback', None)
        if damage is not None:
            damage.close()
        profile = getattr(self, 'controller_profile', None)
        if profile is not None:
            profile.restore()
        failures = set() if attachment_ok else {'camera attachment'}
        view_target = getattr(self, 'view_target_profile', None)
        if view_target is not None:
            view_target.restore()
            if view_target.restore_failures:
                failures.add('local dialogue view target')
        if not native_ok:
            failures.add('native roll instructions')
        cursor = getattr(self, 'cursor_profile', None)
        if cursor is not None:
            cursor.restore()
            if cursor.restore_failures:
                failures.add('native cursor')
        mouse = getattr(self, 'mouse_bindings', None)
        if mouse is not None:
            mouse.restore()
            if mouse.restore_failures:
                failures.add('mouse actions')
        keyboard = getattr(self, 'keyboard_movement', None)
        if keyboard is not None:
            keyboard.restore()
            if keyboard.restore_failures:
                failures.add('keyboard movement')
        combat = getattr(self, 'combat_profile', None)
        if combat is not None:
            combat.restore()
            if combat.restore_failures:
                failures.add('combat aiming')
        silhouettes = getattr(self, 'silhouette_profile', None)
        if silhouettes is not None:
            silhouettes.restore()
            if silhouettes.restore_failures:
                failures.add('mob silhouettes')
        avatar = getattr(self, 'avatar_profile', None)
        if avatar is not None:
            avatar.restore()
            if avatar.restore_failures:
                failures.add('avatar presentation')
        if profile is not None and profile.restore_failures:
            failures.add('controller bindings')
        ordered = list(self.originals.items())
        for address, (obj, saved, mask) in ordered:
            if not attachment_ok and obj == self.camera:
                continue  # Keep a recoverable local view until F5 can reattach.
            try:
                if self.identity(obj) != self.identities[obj]:
                    failures.add(address)
                    continue
                original = saved
                if mask is not None:
                    original = bytes([(self.game.read(address, 1)[0] & ~mask) | (original[0] & mask)])
                self.game.write(address, original)
                if self.game.read(address, len(original)) != original:
                    failures.add(address)
            except (OSError, RuntimeError, ValueError):
                failures.add(address)
        # The spring arm has continued tracking native zones. Rejoin its current
        # socket after restoring relative fields; never replay an old arm snapshot.
        if pose is not None and attachment_ok:
            try:
                pose.restore_world()
            except (OSError, RuntimeError, ValueError):
                failures.add('camera pose')
        self.restore_failures = failures
        if failures:
            print(f'Camera restoration incomplete: {len(failures)} unavailable/changed fields. Press F5 to retry if the game is still open.', flush=True)
            print('Restoration blockers: '+', '.join(sorted(str(field) for field in failures)),flush=True)
        else:
            print('Original camera restored and read back successfully.', flush=True)


class NativeInput:
    """Input for the mod itself; look is only captured while its game has focus."""

    def __init__(self, pid):
        self.pid, self.previous = pid, {}
        self.using_gamepad, self.controller_activity, self.last_mouse = False, False, None
        self.gamepad_index = None
        self.left_stick = (0.,0.)
        self.gamepad_attack_held = False
        self.user = C.WinDLL('user32', use_last_error=True)
        self.user.SetProcessDPIAware()
        signatures = {
            'GetForegroundWindow': ([], W.HWND),
            'GetWindowThreadProcessId': ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            'GetAsyncKeyState': ([C.c_int], C.c_short),
            'GetCursorPos': ([C.POINTER(W.POINT)], W.BOOL),
            'SetCursorPos': ([C.c_int, C.c_int], W.BOOL),
            'GetClientRect': ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            'ClientToScreen': ([W.HWND, C.POINTER(W.POINT)], W.BOOL),
            'RegisterHotKey': ([W.HWND, C.c_int, W.UINT, W.UINT], W.BOOL),
            'UnregisterHotKey': ([W.HWND, C.c_int], W.BOOL),
            'PeekMessageW': ([C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result

        class Gamepad(C.Structure):
            _fields_ = [('buttons', W.WORD), ('leftTrigger', W.BYTE), ('rightTrigger', W.BYTE),
                        ('leftX', C.c_short), ('leftY', C.c_short), ('rightX', C.c_short), ('rightY', C.c_short)]

        class State(C.Structure):
            _fields_ = [('packet', W.DWORD), ('pad', Gamepad)]

        self.State = State
        self.xinput = C.WinDLL('xinput1_4')
        self.xinput.XInputGetState.argtypes = [W.DWORD, C.POINTER(State)]
        self.xinput.XInputGetState.restype = W.DWORD
        self.hotkeys, self.pending = {}, set()
        for identifier, key in ((1, 0x75), (2, 0x79), (3, 0x74), (4, 0x78), (5, 0x73)):
            if self.user.RegisterHotKey(None, identifier, 0x4000, key):
                self.hotkeys[identifier] = key
        if self.user.RegisterHotKey(None, 6, 0x4002, 0x7A):  # Ctrl+F11; avoid fullscreen/screenshot keys.
            self.hotkeys[6] = 0x7A

    def close(self):
        for identifier in self.hotkeys:
            self.user.UnregisterHotKey(None, identifier)

    def pump(self):
        message = W.MSG()
        while self.user.PeekMessageW(C.byref(message), None, 0x0312, 0x0312, 1):
            key = self.hotkeys.get(message.wParam)
            if key is not None:
                self.pending.add(key)

    def foreground(self):
        window, pid = self.user.GetForegroundWindow(), W.DWORD()
        self.user.GetWindowThreadProcessId(window, C.byref(pid))
        self.foreground_pid = pid.value
        return window if pid.value == self.pid else None

    def pressed(self, key):
        if key == 0x7A and key not in self.hotkeys.values():
            return False  # Never treat an unmodified F11 as a diagnostic hotkey.
        if key in self.hotkeys.values():
            pressed = key in self.pending
            self.pending.discard(key)
            return pressed
        down = bool(self.user.GetAsyncKeyState(key) & 0x8000)
        before = self.previous.get(key, False)
        self.previous[key] = down
        return down and not before

    def mouse(self, window, discard=False):
        rect, point = W.RECT(), W.POINT()
        if not self.user.GetClientRect(window, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        center = W.POINT((rect.left + rect.right)//2, (rect.top + rect.bottom)//2)
        if not self.user.ClientToScreen(window, C.byref(center)) or not self.user.GetCursorPos(C.byref(point)):
            raise C.WinError(C.get_last_error())
        position = (point.x, point.y)
        moved = self.last_mouse is not None and position != self.last_mouse
        if self.controller_activity:
            self.using_gamepad = True
        elif self.using_gamepad and moved and not discard:
            self.using_gamepad = False
            discard = True
        if self.using_gamepad:
            self.last_mouse = position
            return 0, 0  # Cursor warps can switch the game back to keyboard input.
        if not self.user.SetCursorPos(center.x, center.y):
            raise C.WinError(C.get_last_error())
        self.last_mouse = (center.x, center.y)
        return (0, 0) if discard else (point.x-center.x, point.y-center.y)

    def gamepad(self):
        state = self.State()
        for index in range(4):
            if self.xinput.XInputGetState(index, C.byref(state)) == 0:
                self.gamepad_index = index
                def axis(value):
                    amount = min(abs(value) / 32767, 1)
                    return math.copysign(min(1., max(0., (amount-.18)/.82)), value)
                self.controller_activity = bool(state.pad.buttons or state.pad.leftTrigger > 30 or
                                                state.pad.rightTrigger > 30 or axis(state.pad.leftX) or
                                                axis(state.pad.leftY) or axis(state.pad.rightX) or axis(state.pad.rightY))
                self.left_stick = (axis(state.pad.leftX),axis(state.pad.leftY))
                # Melee, ranged and artifact/interact buttons get view-facing
                # priority before their native ability becomes active. R3/L3
                # are deliberately excluded from this combat intent.
                self.gamepad_attack_held = bool(state.pad.buttons&0xF300 or
                    state.pad.leftTrigger>30 or state.pad.rightTrigger>30)
                return axis(state.pad.rightX), axis(state.pad.rightY), state.pad.buttons
        self.controller_activity = False
        self.gamepad_index = None
        self.left_stick = (0.,0.)
        self.gamepad_attack_held = False
        return 0, 0, 0

    def keyboard(self, window):
        from keyboard_movement import keyboard_axes
        if not window or self.controller_activity:
            return (0., 0.)
        keys = [bool(self.user.GetAsyncKeyState(key)&0x8000) for key in (0x57, 0x41, 0x53, 0x44)]
        if any(keys):
            self.using_gamepad = False
        return keyboard_axes(*keys)

    def movement(self, window):
        from keyboard_movement import controller_axes
        if not window:
            return (0.,0.)
        return controller_axes(self.left_stick) if self.controller_activity else self.keyboard(window)

    def roll_buttons(self, buttons):
        from roll_direction import KEYBOARD_DODGE
        # Track the held key even without focus so Alt-Tab cannot synthesize a
        # new press. The combat profile separately gates all writes on focus.
        if not self.controller_activity and self.user.GetAsyncKeyState(0x52)&0x8000:
            return buttons | KEYBOARD_DODGE
        return buttons

    def interact_modifier(self, window):
        return bool(window) and not self.controller_activity and bool(self.user.GetAsyncKeyState(0x10)&0x8000)

    def attack_held(self, window):
        if not window:
            return False
        if self.controller_activity:
            return self.gamepad_attack_held
        return any(self.user.GetAsyncKeyState(key)&0x8000
                   for key in (0x01,0x02,0x31,0x32,0x33,0x45))


class SingleInstance:
    def __init__(self, game=None):
        self.kernel = game.kernel if game is not None else C.WinDLL('kernel32', use_last_error=True)
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CloseHandle.restype = W.BOOL
        self.kernel.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
        self.kernel.CreateMutexW.restype = W.HANDLE
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\DungeonsFirstPerson-CameraHelper')
        if not self.handle:
            raise C.WinError(C.get_last_error())
        if C.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            raise RuntimeError('A camera helper is already running for this game. Use its F6/F10 controls.')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.kernel.CloseHandle(self.handle)


def interactive(game, args, stop_event=None):
    controls = NativeInput(game.pid)
    session = last_session = prepared = menu_state = None
    selected = bool(args.start_enabled)
    process_ended = False
    resume_since = None
    saved_view = None
    pause_reason = None
    focus_loss_at = None
    camera_hold = CameraHold()
    quick_turn = QuickTurn()
    diagnostics = None
    if getattr(args, 'coop_diagnostics', False):
        from coop_diagnostics import CoopDiagnostics
        diagnostics = CoopDiagnostics(args)
    discovery = CameraDiscovery()
    recovery = CameraRecovery()
    discovery_retry_at = 0.
    discovery_note = None
    controller_index = None
    stopping, restored = threading.Event(), threading.Event()
    lock = threading.RLock()

    def discover_player(previous_session):
        # Discovery writes nothing. Activation always captures fresh native
        # baselines, including after walking into a different camera zone.
        try:
            seed = CameraSession(game, args.height, args.fov, previous=previous_session)
        except (OSError, RuntimeError, KeyError, StopIteration, ValueError):
            if previous_session is None:
                raise
            seed = CameraSession(game, args.height, args.fov)
        return seed, MenuState(seed)

    def disable(smooth=False, reason=None):
        nonlocal session, last_session, saved_view
        quick_turn.cancel()
        with lock:
            if session is not None:
                if reason is not None:
                    saved_view = (session.pawn, session.yaw, session.pitch)
                if smooth:
                    try:
                        session.animate_out(controls.foreground)
                    except (OSError, RuntimeError, ValueError, KeyError, StopIteration):
                        pass
                session.restore()
                last_session, session = session, None
                if reason is not None:
                    print(f'First person paused for {reason}; it will resume automatically.', flush=True)
                else:
                    print('First person OFF. Press F6 in the game to enable it.', flush=True)

    def recover(error, phase):
        nonlocal selected, prepared, menu_state, resume_since, pause_reason, discovery_retry_at
        wanted = selected
        disable(reason='camera recovery' if wanted else None)
        prepared = menu_state = resume_since = None
        pause_reason = 'camera recovery' if wanted else None
        selected = wanted and recovery.failed(now)
        discovery_retry_at = recovery.retry_at
        print(f'{phase}: {error}',flush=True)
        location = failure_path(error)
        if location:
            print('Failure path: '+location,flush=True)
        if selected:
            print(f'Camera recovery {recovery.attempts}/3: restore first, then rediscover the local player.',flush=True)
        elif wanted:
            print('Automatic camera recovery paused after repeated failures. F6 retries restoration and fresh discovery; F5 restores only.',flush=True)

    handler_type = C.WINFUNCTYPE(W.BOOL, W.DWORD)

    @handler_type
    def console_close(event):
        if event in (0, 1, 2, 5, 6):
            stopping.set()
            restored.wait(2)
            return True
        return False

    game.kernel.SetConsoleCtrlHandler.argtypes = [handler_type, W.BOOL]
    game.kernel.SetConsoleCtrlHandler.restype = W.BOOL
    if not game.kernel.SetConsoleCtrlHandler(console_close, True):
        controls.close()
        raise C.WinError(C.get_last_error())
    print('Ready: F6 / hold L3 for 2 seconds toggle | F10 restore and quit | mouse/right stick look', flush=True)
    print('F4 toggles through-wall mob silhouettes in first person.', flush=True)
    if not getattr(args, 'no_wasd', False):
        print('First person: WASD moves relative to your view; S no longer opens the multiplayer wheel.', flush=True)
        print('Mouse: left-click attacks; Shift+left-click interacts. Native bow timing and targets are retained.', flush=True)
    if diagnostics is not None:
        print('Co-op diagnostics enabled. Ctrl+F11 captures local/remote presentation in this process; ask your friend to verify their view.', flush=True)
    print('F5 restore again | F7/F8 FOV | F9 damage flash preview | Page Down/Up eye height.', flush=True)
    print('First person: R / R3 dodge; WASD / left stick chooses direction. G / LB emotes. Menus resume automatically; Alt-Tab only pauses look input.', flush=True)
    print('Double-click L3 in first person for a smooth 180-degree quick turn.', flush=True)
    if getattr(args, 'native_roll_hook', False):
        print('OPT-IN native roll prototype: offline checked, NOT live-playtested. Late force steering is disabled.', flush=True)
    deadline = time.monotonic()+args.seconds if args.seconds else math.inf
    previous = time.monotonic()
    try:
        while not stopping.is_set() and not (stop_event and stop_event.is_set()) and time.monotonic() < deadline:
            alive = W.DWORD()
            if not game.kernel.GetExitCodeProcess(game.handle, C.byref(alive)) or alive.value != 259:
                process_ended = True
                print(f'Game closed (exit code 0x{alive.value:08X}). Waiting for the next launch; first person will start OFF.', flush=True)
                return 'game_closed'
            now = time.monotonic()
            work_started = time.perf_counter()
            dt, previous = min(now-previous, .05), now
            window = controls.foreground()
            regained_focus = bool(window and focus_loss_at is not None)
            if window:
                focus_loss_at = None
            elif focus_loss_at is None:
                focus_loss_at = now
            controls.pump()
            toggle, quit_requested = controls.pressed(0x75), controls.pressed(0x79)
            reset_requested = controls.pressed(0x74)
            preview_damage = controls.pressed(0x78)
            toggle_silhouettes = controls.pressed(0x73)
            snapshot_requested = controls.pressed(0x7A)
            fov_change = 5*(int(controls.pressed(0x77))-int(controls.pressed(0x76)))
            height_change = 5*(int(controls.pressed(0x21))-int(controls.pressed(0x22)))
            stick_x, stick_y, buttons = controls.gamepad()
            if controls.gamepad_index != controller_index:
                camera_hold.cancel()
                quick_turn.cancel()
                controller_index = controls.gamepad_index
            held_toggle = camera_hold.update(buttons, now, eligible=bool(window) and pause_reason is None
                                            and not (menu_state and menu_state.input_captured is True))
            toggle = toggle or held_toggle
            if quit_requested:
                break
            if reset_requested:
                camera_hold.cancel()
                selected = False
                disable()
                if last_session is not None and last_session.restore_failures:
                    last_session.restore()
                prepared = menu_state = saved_view = resume_since = pause_reason = None
                recovery.reset()
            elif toggle and window:
                needs_fresh = recovery.fresh or recovery.attempts > 0
                recovery.reset()
                recovery.fresh = needs_fresh
                selected = not selected
                if selected and needs_fresh:
                    prepared = menu_state = None
                    discovery_retry_at = 0.
                if not selected:
                    disable(smooth=True)
                    prepared = saved_view = resume_since = pause_reason = None
                    print('Automatic first-person resume cancelled.', flush=True)

            if toggle_silhouettes and window:
                args.keep_mob_silhouettes = not getattr(args, 'keep_mob_silhouettes', False)
                state = 'SHOWN' if args.keep_mob_silhouettes else 'HIDDEN'
                print(f'First-person mob silhouettes: {state}.', flush=True)

            if session is None and menu_state is None:
                if window and now >= discovery_retry_at:
                    # Recovery discards reflection/object snapshots. Ordinary
                    # menu transitions can still reuse guarded player discovery.
                    prior = None if recovery.fresh else last_session
                    discovery.start(lambda previous_session=prior: discover_player(previous_session))
                warm, error = discovery.poll()
                if warm is not None:
                    _, menu_state = warm
                    discovery_note = None
                    print('Camera discovery ready; toggles now use cached player data.', flush=True)
                elif error is not None:
                    discovery_retry_at = now+3.
                    if selected and str(error) != discovery_note:
                        discovery_note = str(error)
                        print(f'Waiting for a playable local player: {error}. First-person selection retained.', flush=True)

            if selected and menu_state is not None:
                try:
                    reason = menu_state.reason() if menu_state is not None else None
                    if reason:
                        details = ', '.join(n.split('_C_')[0] for n in menu_state.names) if reason == 'menu' else ''
                        disable(reason=f'{reason}: {details}' if details else reason)
                        prepared = None  # Capture the native camera after the menu closes.
                        if pause_reason != reason and session is None:
                            print(f'First person selected; waiting for {reason} to close.', flush=True)
                        pause_reason, resume_since = reason, None
                    elif session is None and window:
                        # Let the native menu's closing animation/input handoff
                        # settle. Initial F6 activation need not wait.
                        if resume_since is None:
                            resume_since = now
                        if pause_reason is None or now-resume_since >= .12:
                            with lock:
                                if last_session is not None and last_session.restore_failures:
                                    last_session.restore()
                                    if last_session.restore_failures:
                                        raise RuntimeError('Restoration still incomplete; no new camera writes allowed')
                                session = CameraSession(game, args.height, args.fov,
                                                        previous=menu_state.session)
                                prepared = None
                                if saved_view is not None and saved_view[0] == session.pawn:
                                    session.yaw, session.pitch = saved_view[1:]
                                if not getattr(args, 'no_avatar', False):
                                    if getattr(args, 'native_avatar_facing', False):
                                        session.enable_avatar_profile(follow_view=False)
                                    else:
                                        session.enable_avatar_profile()
                                if not getattr(args, 'no_damage_flash', False):
                                    try:
                                        session.enable_damage_feedback()
                                    except (OSError, RuntimeError, KeyError, StopIteration, ValueError) as error:
                                        print(f'Damage feedback unavailable here: {error}', flush=True)
                                if not getattr(args, 'keep_mob_silhouettes', False):
                                    try:
                                        session.set_silhouette_hiding(menu_state, True)
                                    except (OSError, RuntimeError, KeyError, StopIteration, ValueError) as error:
                                        print(f'Mob silhouette suppression unavailable: {error}', flush=True)
                                session.enable_controller_profile()
                                if not getattr(args, 'no_wasd', False):
                                    session.enable_keyboard_movement()
                                    session.enable_mouse_bindings()
                                if not getattr(args, 'native_combat', False):
                                    steer_roll = not (getattr(args, 'native_roll_direction', False) or
                                                      getattr(args, 'roll_target_data', False) or
                                                      getattr(args, 'native_roll_hook', False))
                                    filter_ranged = (getattr(args, 'filter_ranged_targets', False) and
                                                     not getattr(args, 'native_ranged_targets', False))
                                    session.enable_combat_profile(steer_roll=steer_roll, filter_ranged=filter_ranged)
                                if getattr(args, 'native_roll_hook', False):
                                    session.enable_native_roll_profile()
                                if getattr(args, 'roll_target_data', False):
                                    session.enable_roll_target_profile()
                                session.hide_cursor()
                                session.set_ui_input(menu_state.input_captured is True)
                                if diagnostics is not None:
                                    diagnostics.track(session, menu_state)
                                session.transition_to(1., start=0.)
                                menu_state.session = session
                                if menu_state.input_captured is not True:
                                    controls.mouse(window, discard=True)
                            print('First person resumed.' if pause_reason else 'First person ON.', flush=True)
                            pause_reason, resume_since = None, None
                except (OSError, RuntimeError, KeyError, StopIteration, ValueError) as error:
                    recover(error,'Cannot resume here')

            if session:
                try:
                    with lock:
                        input_captured = menu_state.input_captured is True
                        session.dialogue_active = input_captured and any(name.split('_C_')[0] in (
                            'W_QuestDialogue_Activatable', 'W_QuestAcceptance_Activatable')
                            for name in menu_state.names)
                        was_captured = session.input_paused is True
                        session.set_ui_input(input_captured)
                        input_window = window if not input_captured else None
                        if toggle_silhouettes and window:
                            session.set_silhouette_hiding(menu_state, not args.keep_mob_silhouettes)
                        turn_delta = quick_turn.update(buttons, now, eligible=bool(input_window) and selected
                                                       and session.blend >= .99 and session.blend_target == 1.)
                        if quick_turn.triggered or quick_turn.active:
                            camera_hold.cancel()  # A quick-turn click cannot become a camera toggle.
                        if quick_turn.triggered:
                            print('L3 quick turn: 180 degrees.', flush=True)
                        if input_window:
                            dx, dy = controls.mouse(input_window, discard=regained_focus or was_captured)
                            session.yaw = (session.yaw+turn_delta+dx*args.sensitivity+stick_x*150*dt+180)%360-180
                            session.pitch = max(-80, min(80, session.pitch-dy*args.sensitivity+stick_y*150*dt))
                        if window and (fov_change or height_change):
                            session.tune(session.fov+fov_change, session.height+height_change)
                            args.fov, args.height = session.fov, session.height
                            print(f'FOV {session.fov:g}; height {session.height:g}', flush=True)
                        movement_axes = controls.movement(input_window)
                        roll_target = getattr(session, 'roll_target_profile', None)
                        if roll_target is not None:
                            roll_target.set_input(movement_axes, eligible=bool(input_window))
                        if session.combat_profile is not None:
                            session.combat_profile.set_input(controls.roll_buttons(buttons), movement_axes,
                                eligible=bool(input_window),attack_held=controls.attack_held(input_window))
                        if getattr(session, 'native_roll_profile', None) is not None:
                            session.native_roll_profile.set_input(bool(input_window))
                        if session.keyboard_movement is not None:
                            session.keyboard_movement.set_input(movement_axes, eligible=bool(input_window))
                        if session.mouse_bindings is not None:
                            session.mouse_bindings.set_input(controls.interact_modifier(input_window))
                        session.update()
                        recovery.healthy(now)
                        if diagnostics is not None and session.blend >= .99:
                            diagnostics.update(session, menu_state, forced=snapshot_requested and bool(window))
                        if window and session.damage_feedback is not None:
                            if preview_damage:
                                session.damage_feedback.preview()
                            session.damage_feedback.update(window)
                except (OSError, RuntimeError, KeyError, StopIteration, ValueError) as error:
                    recover(error,'Camera suspended')
            else:
                quick_turn.cancel()
            if session is not None and diagnostics is not None:
                diagnostics.observe_frame(time.perf_counter()-work_started)
            # Work belongs inside the 120 Hz budget. Adding a whole frame of
            # sleep after every update made ordinary turning run at 50-60 Hz.
            time.sleep(max(0., 1/120-(time.perf_counter()-work_started)))
    finally:
        discovery.close()
        if not process_ended:
            disable()
        elif session is not None and session.damage_feedback is not None:
            session.damage_feedback.close()
        native_manager = vars(game).get('native_roll_hook')
        if not process_ended and native_manager is not None:
            native_manager.restore()
            if native_manager.restore_failures:
                print('ERROR: Native roll instruction restoration incomplete; game restart resets transient patches.', flush=True)
        target_manager = vars(game).get('roll_target_data_hook')
        if not process_ended and target_manager is not None:
            target_manager.restore()
            if target_manager.restore_failures:
                print('ERROR: Roll target-data instruction restoration incomplete; use F5.', flush=True)
        restored.set()
        game.kernel.SetConsoleCtrlHandler(console_close, False)
        controls.close()
    return 'quit'


def run_live(args, stop_event=None):
    """Keep the launcher alive across process restarts, discarding all old addresses."""
    auto_start = args.start_enabled
    while True:
        controls = NativeInput(0)
        next_scan = 0
        pid = None
        try:
            print('Waiting for Minecraft Dungeons II. Start it normally; F10 exits this helper.', flush=True)
            while pid is None:
                if stop_event is not None and stop_event.is_set():
                    return
                controls.pump()
                if controls.pressed(0x79):
                    return
                now = time.monotonic()
                if now >= next_scan:
                    pid = find_game_pid()
                    next_scan = now+1
                if pid is None:
                    time.sleep(.1)
        finally:
            controls.close()
        try:
            with CameraMemory(pid) as game:
                args.start_enabled = auto_start
                auto_start = False
                result = interactive(game, args, stop_event=stop_event)
        except OSError as error:
            print(f'Game not ready: {error}. Retrying connection.', flush=True)
            time.sleep(.5)
            continue
        if result != 'game_closed':
            return


def main(argv=None, stop_event=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=0, help='Optional test timeout; interactive defaults to unlimited')
    parser.add_argument('--height', type=float, default=55)
    parser.add_argument('--fov', type=float, default=95)
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--start-enabled', action='store_true', help='Enable once the game has focus')
    parser.add_argument('--no-avatar', action='store_true', help='Camera/controller only, without arms-only visuals or mesh rotation')
    parser.add_argument('--native-combat', action='store_true', help='Keep native movement-based combat aiming for comparison')
    parser.add_argument('--native-ranged-targets', action='store_true', help='Keep native bow targets (now the default); overrides experimental filtering')
    parser.add_argument('--filter-ranged-targets', action='store_true', help='Experimental old ranged filter; caused animation-only shots in guest co-op')
    parser.add_argument('--no-wasd', action='store_true', help='Keep native mouse/controller movement, mouse actions and S menu binding for comparison')
    parser.add_argument('--native-avatar-facing', action='store_true', help='Hide the body without aligning the mesh to camera yaw')
    parser.add_argument('--native-roll-direction', action='store_true', help='Keep camera-facing body direction instead of facing ordinary movement before dodges')
    parser.add_argument('--native-roll-hook', action='store_true', help='Disabled after reversed travel and a game exit in the live co-op trial')
    parser.add_argument('--roll-target-data', action='store_true', help='Choose the local roll direction before native target-data commitment')
    parser.add_argument('--coop-diagnostics', action='store_true', help='Read-only local log snapshots; Ctrl+F11 captures while first person is active')
    parser.add_argument('--keep-mob-silhouettes', action='store_true', help='Start with native through-wall mob silhouettes; F4 toggles in game')
    parser.add_argument('--no-damage-flash', action='store_true', help='Disable the undirected health-loss pulse')
    parser.add_argument('--sensitivity', type=float, default=.12, help='Mouse degrees per pixel')
    args = parser.parse_args(argv)
    if not (0 <= args.seconds <= 300 and 10 <= args.height <= 120 and 65 <= args.fov <= 115 and .01 <= args.sensitivity <= 2):
        parser.error('seconds 0-300, height 10-120, FOV 65-115 and sensitivity 0.01-2 required')
    from build_info import NATIVE_ROLL_AVAILABLE, ROLL_TARGET_DATA_AVAILABLE
    if args.roll_target_data and not ROLL_TARGET_DATA_AVAILABLE:
        parser.error('Roll target-data experiment disabled after a game exit in the live trial')
    if args.native_roll_hook and not NATIVE_ROLL_AVAILABLE:
        parser.error('Native roll experiment disabled after reversed co-op travel and a game exit')
    if args.native_roll_hook and (not args.interactive or args.native_combat or args.native_roll_direction or args.no_wasd):
        parser.error('--native-roll-hook requires --interactive, first-person controls and movement; do not combine with --native-roll-direction')
    if args.roll_target_data and (not args.interactive or args.native_combat or args.native_roll_direction or args.no_wasd or args.native_roll_hook):
        parser.error('--roll-target-data requires interactive first-person movement and combat controls')
    with SingleInstance():
        if args.interactive:
            run_live(args, stop_event=stop_event)
            return
        with CameraMemory() as game:
            seconds = args.seconds or 10
            session = CameraSession(game, args.height, args.fov)
            print(f'Applying camera-only test for {seconds:g}s to PID {game.pid}; no loader or executable patches.', flush=True)
            try:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    session.update()
                    time.sleep(1 / 120)
            finally:
                session.restore()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, KeyboardInterrupt) as error:
        print(f'Camera test ended: {error}', file=sys.stderr)
        sys.exit(1)
