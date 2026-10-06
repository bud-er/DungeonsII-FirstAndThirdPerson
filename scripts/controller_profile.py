"""Temporary, reversible Enhanced Input remapping for the local player.

Swapping complete FKeys preserves ownership of their cached shared pointers.
Array headers and row identities are checked before touching array storage.
"""
import struct
import time
from keyboard_movement import DisabledKey
from input_mapping_snapshot import mapping_snapshot, MappingRebuilt


class KeySwap:
    def __init__(self, profile, owner, header, descriptor, first, second, inherited=False):
        self.profile, self.owner, self.header, self.descriptor = profile, owner, header, descriptor
        self.identity = profile.camera.identity(owner)
        self.rows = (first['address'], second['address'])
        self.tags = (first['tag'], second['tag'])
        keys = (first['keyBytes'], second['keyBytes'])
        self.original = keys[::-1] if inherited else keys
        self.desired = self.original[::-1]

    def valid(self):
        p, c = self.profile, self.profile.camera
        try:
            return (c.identity(self.owner) == self.identity and
                    p.game.read(self.header, 16) == self.descriptor and
                    all(p.game.read(row, p.key_offset) == tag for row, tag in zip(self.rows, self.tags)))
        except (OSError, RuntimeError, ValueError):
            return False

    def write(self, values):
        if not self.valid():
            return False
        addresses = [row+self.profile.key_offset for row in self.rows]
        for index, address in enumerate(addresses):
            current = self.profile.game.read(address, 24)
            if current not in (self.original[index], self.desired[index]):
                raise RuntimeError('A dodge binding changed outside the helper; refusing to overwrite it')
        for address, value in zip(addresses, values):
            if self.profile.game.read(address, 24) != value:
                self.profile.game.write(address, value)
            if self.profile.game.read(address, 24) != value:
                raise RuntimeError('Controller binding write did not persist')
        return True


class ControllerProfile:
    def __init__(self, camera):
        self.camera, self.session, self.game = camera, camera, camera.game
        for name in ('EnhancedPlayerInput', 'InputMappingContext', 'EnhancedActionKeyMapping', 'InputTrigger', 'Key'):
            camera.ensure_layout(name)
        layout = camera.layouts['EnhancedActionKeyMapping']
        fields = {p['name']: p for p in layout['properties']}
        self.stride = layout['size']
        self.key_offset = fields['Key']['offset']
        self.action_offset = fields['Action']['offset']
        self.trigger_offset = fields['Triggers']['offset']
        if (self.stride, self.key_offset, self.action_offset, self.trigger_offset,
            fields['Key']['size'], camera.layouts['Key']['size']) != (0x50, 0x28, 0x20, 0, 24, 24):
            raise RuntimeError('Enhanced Input layout differs from the verified build')
        self.sources = [o['address'] for o in camera.objects
                        if o['name'] in ('IMC_DefaultGamepad', 'IMC_SwitchGamepad')
                        and not o['flags'] & 0x30
                        and camera.is_a(o['class'], 'InputMappingContext')]
        if not self.sources:
            raise RuntimeError('Gamepad mapping contexts are not loaded yet')
        self.swaps, self.thresholds = {}, {}
        self.source_identities = {obj: camera.identity(obj) for obj in self.sources}
        self.axis_blocks = {}
        self.axis_key = self.axis_cache = None
        self.axis_caches = set()
        self.r3_key = self.r3_cache = None
        self.active_thresholds = set()
        self.last_update = 0
        self.default_threshold = None
        self.ui_paused = False
        # Read the untouched source contexts before editing any runtime copies.
        self.collect(include_runtime=False)
        self.collect()

    def array(self, owner, cls, name):
        address = self.camera.field(owner, cls, name)['address']
        owner_identity = self.camera.identity(owner)
        descriptor, data = mapping_snapshot(self.game, address, self.stride)
        pointer, count, capacity = struct.unpack('<Qii', descriptor)
        rows = []
        for index in range(count):
            row_address = pointer + index*self.stride
            raw = data[index*self.stride:(index+1)*self.stride]
            action = struct.unpack_from('<Q', raw, self.action_offset)[0]
            if not action:
                continue
            key_bytes = raw[self.key_offset:self.key_offset+24]
            rows.append({'address': row_address, 'tag': raw[:self.key_offset],
                         'action': self.camera.reflection.header(action)['name'],
                         'key': self.camera.reflection.name(*struct.unpack_from('<II', key_bytes)),
                         'keyBytes': key_bytes, 'raw': raw})
        if self.game.read(address, 16) != descriptor or self.camera.identity(owner) != owner_identity:
            raise MappingRebuilt('Controller mappings changed during discovery')
        return address, descriptor, rows

    def capture(self, owner, cls, name, source=False):
        header, descriptor, rows = self.array(owner, cls, name)
        if hasattr(self, '_collecting_arrays'):
            self._collecting_arrays.append((owner, self.camera.identity(owner), header, descriptor))
        caches = getattr(self, 'axis_caches', set())
        if self.axis_cache is not None:
            caches.add(self.axis_cache)
        for row in rows:
            if row['action'] == 'IA_DodgeDirectional' and row['key'] == 'Gamepad_Right2D':
                caches.add(row['keyBytes'][8:])
            elif row['action'] == 'IA_DodgeDirectional' and row['key'] == 'None':
                # Native FKey caches can fill in while our name is disabled.
                # Recognize only a row we already own, with the same object,
                # allocation and action/trigger identity; never guess a new None.
                for block in self.axis_blocks.values():
                    if (block.owner == owner and block.descriptor == descriptor and
                            block.row == row['address'] and block.tag == row['tag'] and
                            block.original == self.axis_key and
                            self.camera.identity(owner) == block.identity):
                        caches.add(row['keyBytes'][8:])
                        break
        self.axis_caches = caches
        directional = [r for r in rows if r['action'] == 'IA_DodgeDirectional' and
                       (r['key'] == 'Gamepad_Right2D' or
                        (r['key'] == 'None' and r['keyBytes'][8:] in caches))]
        if not directional:
            return  # Keyboard contexts do not have a right-stick dodge.
        forward = [r for r in rows if r['action'] == 'IA_DodgeForward' and r['key'] in ('Gamepad_LeftShoulder', 'Gamepad_RightThumbstick')]
        emotes = [r for r in rows if r['action'] == 'IA_OpenUI_Emotes' and r['key'] in ('Gamepad_LeftShoulder', 'Gamepad_RightThumbstick')]
        complete_pair = (len(forward) == 1 and len(emotes) == 1 and
                         forward[0]['key'] != emotes[0]['key'])
        if source and (len(directional) != 1 or not complete_pair):
            raise RuntimeError('Custom controller bindings differ from the supported dodge/emote pair')
        # Threshold-only suppression did not stop axis-created rolls in the
        # live guest session after the failed native-input experiments. Remove
        # the key name as well. Preserve its owning cache and the native action.
        axis = directional[0]
        if source and axis['key'] == 'Gamepad_Right2D':
            original, cache = axis['keyBytes'][:8], axis['keyBytes'][8:]
            if self.axis_key not in (None, original):
                raise RuntimeError('Controller contexts use different right-axis keys')
            self.axis_key, self.axis_cache = original, cache
        if self.axis_key is None:
            raise RuntimeError('Original right-axis binding is unavailable')
        # Axis suppression must not depend on the R3/emote pair being present.
        # Runtime rebuilds can temporarily omit or duplicate unrelated rows.
        for axis_row in directional:
            if axis_row['key'] != 'None' and axis_row['keyBytes'][:8] != self.axis_key:
                raise RuntimeError('Runtime right-axis key differs from the verified source')
            block_key = (owner, descriptor, axis_row['address'], axis_row['tag'],
                         axis_row['raw'][0x30:0x40] + axis_row['raw'][0x41:])
            if block_key not in self.axis_blocks:
                self.axis_blocks[block_key] = DisabledKey(self, owner, header, descriptor,
                    axis_row['address'], self.axis_key, axis_row['raw'])
        if complete_pair:
            key = (owner, descriptor, forward[0]['address'], forward[0]['tag'], emotes[0]['tag'])
            if key not in self.swaps:
                self.swaps[key] = KeySwap(self, owner, header, descriptor, forward[0], emotes[0],
                                         inherited=forward[0]['key'] == 'Gamepad_RightThumbstick')
        # The failed prototype left an additional directional R3 row in the
        # running input array, even though neither source context has it.
        # R3 must reach only the confirmed forward action/force-steering path.
        # Block such duplicate rows without changing their action or axes.
        for row in forward + emotes:
            if row['key'] == 'Gamepad_RightThumbstick':
                self.r3_key, self.r3_cache = row['keyBytes'][:8], row['keyBytes'][8:]
        for row in rows:
            if row['action'] != 'IA_DodgeDirectional':
                continue
            if row['key'] != 'Gamepad_RightThumbstick' and not (
                    row['key'] == 'None' and self.r3_cache is not None and
                    row['keyBytes'][8:] == self.r3_cache):
                continue
            duplicate_key = (owner, descriptor, row['address'], row['tag'])
            if duplicate_key not in self.axis_blocks:
                self.axis_blocks[duplicate_key] = DisabledKey(self, owner, header, descriptor,
                                                             row['address'], self.r3_key, row['raw'])
        pointer, count, capacity = struct.unpack_from('<Qii', directional[0]['raw'], self.trigger_offset)
        if count != 1 or not 1 <= capacity <= 32:
            if not source:
                return  # The key-name block already suppresses this runtime axis.
            raise RuntimeError('Directional dodge trigger is not the verified single pressed trigger')
        trigger = self.game.u64(pointer)
        # Trigger instances can be recycled between input-context rebuilds.
        # Do not classify a new instance through the old cached UObject header.
        self.camera.reflection.headers.pop(trigger, None)
        if not self.camera.is_a(self.camera.reflection.header(trigger)['class'], 'InputTriggerPressed'):
            if not source:
                return  # Preserve unfamiliar native triggers; suppress only the key.
            raise RuntimeError('Unexpected directional dodge trigger type')
        field = self.camera.field(trigger, 'InputTrigger', 'ActuationThreshold')
        if field['type'] != 'FloatProperty' or field['size'] != 4:
            raise RuntimeError('Unexpected dodge threshold layout')
        identity = self.camera.identity(trigger)
        threshold_key = (trigger, identity)
        if threshold_key not in self.thresholds:
            original = self.game.read(field['address'], 4)
            value = struct.unpack('<f', original)[0]
            if value == 10 and self.default_threshold is not None:
                original = self.default_threshold  # Runtime clone of an edited context.
            elif not 0 < value <= 2:
                raise RuntimeError('Unexpected original dodge threshold')
            if source:
                if self.default_threshold not in (None, original):
                    raise RuntimeError('Controller contexts use different dodge thresholds')
                self.default_threshold = original
            self.thresholds[threshold_key] = (field['address'], original)
        return threshold_key

    def collect(self, include_runtime=True):
        self._collecting_arrays = []
        try:
            self._collect(include_runtime)
        finally:
            del self._collecting_arrays

    def _collect(self, include_runtime=True):
        active = set()
        for source in self.sources:
            expected = getattr(self, 'source_identities', {}).get(source)
            if expected is not None:
                try:
                    if self.camera.identity(source) != expected:
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue  # Retired source allocations cannot be restored or edited.
            key = self.capture(source, 'InputMappingContext', 'Mappings', source=True)
            if key is not None:
                active.add(key)
        if include_runtime:
            player_input = self.camera.pointer(self.camera.pc, 'PlayerController', 'PlayerInput')
            if player_input and self.camera.is_a(self.camera.reflection.header(player_input)['class'], 'EnhancedPlayerInput'):
                key = self.capture(player_input, 'EnhancedPlayerInput', 'EnhancedActionMappings')
                if key is not None:
                    active.add(key)
        # Publish only after a complete snapshot. A failed read must not make a
        # still-active trigger look retired and turn a real failure into success.
        arrays = tuple(self._collecting_arrays)
        if not self.mappings_valid(arrays):
            raise MappingRebuilt('Controller mapping owners changed during collection')
        self.mapping_arrays = arrays
        self.active_thresholds = active

    def mappings_valid(self, arrays=None):
        try:
            return all(self.camera.identity(owner) == identity and
                       self.game.read(header, 16) == descriptor
                       for owner, identity, header, descriptor in
                       (getattr(self, 'mapping_arrays', ()) if arrays is None else arrays))
        except (OSError, RuntimeError, ValueError):
            return False

    def update(self):
        now = time.monotonic()
        if (now-self.last_update < .04 and self.mappings_valid() and
                all(record.valid() for record in self.axis_blocks.values())):
            # Re-check/reassert known axis rows every frame; a reset must not
            # wait for the slower full mapping scan.
            return self.enforce_axes()
        try:
            self.collect()
        except MappingRebuilt:
            self.last_update = 0.
            return False  # Keep the view; retry without writing this frame's rows.
        # Old runtime arrays accumulated across every menu rebuild. Their
        # serial/header/tag guards already refuse writes; stop revisiting them
        # on every input tick once a complete current snapshot is available.
        self.swaps = {key: record for key, record in self.swaps.items() if record.valid()}
        self.axis_blocks = {key: record for key, record in self.axis_blocks.items() if record.valid()}
        for key, (address, original) in list(self.thresholds.items()):
            if key in self.active_thresholds:
                continue
            obj, identity = key
            try:
                valid = self.camera.identity(obj) == identity
            except (OSError, RuntimeError, ValueError):
                valid = False  # Complete current snapshot no longer references it.
            if valid:
                current = self.game.read(address, 4)
                if current == struct.pack('<f', 10):
                    self.game.write(address, original)
                    if self.game.read(address, 4) != original:
                        raise RuntimeError('Retired dodge threshold restoration failed')
            self.thresholds.pop(key)
        # Disable axis dodge before assigning click-to-dodge. This threshold is
        # above any normalized stick vector and is copied into rebuilt contexts.
        for (obj, identity), (address, original) in self.thresholds.items():
            if (obj, identity) not in self.active_thresholds:
                continue
            try:
                valid = self.camera.identity(obj) == identity
            except (OSError, RuntimeError, ValueError):
                valid = False
            if valid:
                desired = struct.pack('<f', 10)
                current = self.game.read(address, 4)
                if current not in (original, desired):
                    raise RuntimeError('Dodge threshold changed outside the helper')
                if current != desired:
                    self.game.write(address, desired)
        if not self.enforce_axes():
            return False
        for swap in self.swaps.values():
            if not swap.write(swap.original if getattr(self, 'ui_paused', False) else swap.desired):
                self.last_update = 0.
                return False
        self.last_update = now
        return True

    def enforce_axes(self):
        if not self.mappings_valid():
            self.last_update = 0.
            return False
        for block in self.axis_blocks.values():
            if not block.disable() or not block.valid():
                self.last_update = 0.
                return False
            if self.game.read(block.row + self.key_offset, 8) != bytes(8):
                raise RuntimeError('Right-stick dodge suppression did not persist')
        if not self.mappings_valid():
            self.last_update = 0.
            return False
        return True

    def set_ui_input(self, captured):
        # Keep the gameplay axis blocked while menus rebuild native mappings.
        # Restore the button pair for menu use, without reopening stick dodge.
        self.ui_paused = bool(captured)
        self.last_update = 0.
        return self.update()

    def restore(self):
        failures, retired = 0, 0
        collected = False
        try:
            self.collect()  # Include a runtime copy rebuilt since the last frame.
            collected = True
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration):
            failures += 1
        for block in getattr(self, 'axis_blocks', {}).values():
            try:
                if not block.restore():
                    failures += 1
            except (OSError, RuntimeError, ValueError):
                failures += 1
        for swap in reversed(list(self.swaps.values())):
            try:
                if not swap.write(swap.original):
                    retired += 1  # Rebuilt/freed rows must never be written.
            except (OSError, RuntimeError, ValueError):
                failures += 1
        for (obj, identity), (address, original) in self.thresholds.items():
            active = not collected or (obj, identity) in self.active_thresholds
            try:
                if self.camera.identity(obj) == identity:
                    current = self.game.read(address, 4)
                    if current == struct.pack('<f', 10):
                        self.game.write(address, original)
                        if self.game.read(address, 4) != original:
                            failures += 1
                elif active:
                    failures += 1
                else:
                    retired += 1
            except (OSError, RuntimeError, ValueError):
                if active:
                    failures += 1
                else:
                    # A runtime trigger removed by an input-context rebuild can
                    # already be freed. It is not a failed live binding restore.
                    retired += 1
        self.restore_failures = failures
        self.retired_records = retired
        print(f'Controller bindings: {failures} restoration failures; {retired} retired records skipped.', flush=True)
