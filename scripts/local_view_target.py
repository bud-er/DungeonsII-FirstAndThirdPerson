"""Keep this client's player camera when villager dialogue selects its NPC.

Only the non-replicated, locally owned camera manager's reflected view-target
slots change. NPCs, other cameras, transforms and native functions are untouched.
The engine continues calculating the view from the existing player component.
"""
import struct

from camera_transition import unreal_rotator


class LocalViewTarget:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.records, self.restore_failures = {}, set()
        self.seen_dialogue = False
        for name in ('PlayerCameraManager', 'TViewTarget', 'MinimalViewInfo'):
            session.ensure_layout(name)
        self.manager = session.pointer(session.pc, 'PlayerController', 'PlayerCameraManager')
        session.remember(self.manager, 'PlayerCameraManager')
        self.identity = session.identity(self.manager)
        fields = {p['name']: p for p in session.layouts['PlayerCameraManager']['properties']}
        target = next(p for p in session.layouts['TViewTarget']['properties'] if p['name'] == 'Target')
        pov = next(p for p in session.layouts['TViewTarget']['properties'] if p['name'] == 'POV')
        state = next(p for p in session.layouts['TViewTarget']['properties'] if p['name'] == 'PlayerState')
        if (target['type'], target['offset'], target['size'], target.get('target'),
            pov['type'], pov['offset'], pov.get('target')) != (
                'ObjectProperty', 0, 8, 'Actor', 'StructProperty', 0x10, 'MinimalViewInfo'):
            raise RuntimeError('Unsupported local view-target layout')
        if (state['type'], state['offset'], state['size'], state.get('target')) != ('ObjectProperty', 0x8D0, 8, 'PlayerState'):
            raise RuntimeError('Unsupported view-target player-state layout')
        self.state_offset = state['offset']
        view = {p['name']: p for p in session.layouts['MinimalViewInfo']['properties']}
        for name, offset, size, kind in (('Location', 0, 24, 'StructProperty'),
                                       ('Rotation', 24, 24, 'StructProperty'),
                                       ('FOV', 48, 4, 'FloatProperty'),
                                       ('DesiredFOV', 52, 4, 'FloatProperty')):
            if (view[name]['offset'], view[name]['size'], view[name]['type']) != (offset, size, kind):
                raise RuntimeError('Unsupported native view pose layout')
        self.slots = []
        for name in ('ViewTarget', 'PendingViewTarget'):
            prop = fields[name]
            if prop['type'] != 'StructProperty' or prop.get('target') != 'TViewTarget' or \
                    prop['size'] != session.layouts['TViewTarget']['size'] or prop['flags'] & 0x20:
                raise RuntimeError('Unsupported or replicated local view target')
            self.slots.append(self.manager+prop['offset'])
        self.validate()

    def validate(self):
        s = self.session
        if s.identity(self.manager) != self.identity or \
                s.pointer(s.pc, 'PlayerController', 'PlayerCameraManager') != self.manager or \
                s.pointer(self.manager, 'PlayerCameraManager', 'PCOwner') != s.pc:
            raise RuntimeError('Local camera-manager ownership changed')
        prop = s.field(self.manager, 'Actor', 'bReplicates')
        if self.game.read(prop['address'], 1)[0] & prop['bool']['byteMask']:
            raise RuntimeError('Refusing a replicated camera manager')

    def villager(self, actor):
        if not actor or actor == self.session.pawn:
            return False
        s = self.session
        s.reflection.headers.pop(actor, None)
        header = s.reflection.header(actor)
        cls = header['class']
        # Live dialogue selected a MerchantVillager, derived from QuestCharacter.
        # Refuse arbitrary actors/cinematics and all player characters.
        return ('Villager' in s.reflection.header(cls)['name'] and
                s.is_a(cls, 'QuestCharacter') and not s.is_a(cls, 'PlayerCharacter'))

    def reason(self):
        self.validate()
        current, pending = (self.game.u64(address) for address in self.slots)
        for actor in (current, pending):
            if actor not in (0, self.session.pawn) and not self.villager(actor):
                return 'game view target'
        if not current:
            return 'game view target'
        return None

    def update(self, dialogue_active):
        self.validate()
        s, g = self.session, self.game
        if dialogue_active:
            self.seen_dialogue = True
        elif self.seen_dialogue:
            # Native dialogue close requests the pawn again. Never replay the
            # old NPC target after the widget's lifetime has ended.
            if all(g.u64(address) in (0, s.pawn) for address in self.slots):
                self.records.clear()
                self.seen_dialogue = False
        for address in self.slots:
            actor = g.u64(address)
            if actor in (0, s.pawn):
                continue
            if not self.villager(actor):
                raise RuntimeError('The game selected another view target')
            identity = s.identity(actor)
            state = g.u64(address+self.state_offset)
            state_identity = s.identity(state) if state else None
            self.records[address] = (actor, identity, state, state_identity)
            # Recheck the slot and actor before editing this local manager.
            if s.identity(actor) != identity or g.u64(address) != actor:
                continue
            g.write(address, struct.pack('<Q', s.pawn))
            if g.u64(address) != s.pawn:
                raise RuntimeError('Local dialogue view-target write did not persist')
            # Seed only this target's plain pose to avoid blending from its NPC
            # pose. Subsequent frames use the engine's player-camera calculation.
            transform = struct.unpack('<12d', g.read(s.camera+0x1E0, 0x60))
            rotation = unreal_rotator(transform[:4])
            g.write(address+0x10, struct.pack('<ddddddff', *transform[4:7],
                                            *rotation, s.fov, s.fov))
            print('Villager dialogue view retained on the local player camera.', flush=True)

    def restore(self):
        self.restore_failures.clear()
        try:
            self.validate()
            for address, (actor, identity, state, state_identity) in list(self.records.items()):
                if self.game.u64(address) != self.session.pawn:
                    self.records.pop(address)  # Native code selected something else.
                    continue
                if self.session.identity(actor) != identity:
                    self.records.pop(address)  # Never resurrect a recycled actor.
                    continue
                if state and self.session.identity(state) != state_identity:
                    self.restore_failures.add(address)
                    continue
                # Native pawn-camera calculation can refresh this tracking
                # pointer. Return its original NPC metadata with the target,
                # so the engine does not follow the pawn again on the next tick.
                if self.game.u64(address+self.state_offset) != state:
                    self.game.write(address+self.state_offset, struct.pack('<Q', state))
                if self.game.u64(address+self.state_offset) != state:
                    self.restore_failures.add(address)
                    continue
                self.game.write(address, struct.pack('<Q', actor))
                if self.game.u64(address) != actor:
                    self.restore_failures.add(address)
                else:
                    self.records.pop(address)
        except (OSError, RuntimeError, ValueError):
            self.restore_failures.add('local dialogue view target')
