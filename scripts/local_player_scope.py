"""Refuse gameplay writes to a remote player or a stale possession.

Authority also describes an offline pawn; it does not prove a network session.
Restoration deliberately uses the existing object/allocation guards instead of
this eligibility check, so losing possession can still restore live objects.
"""

ROLE_NAMES = {0: 'none', 1: 'simulated proxy', 2: 'autonomous proxy (guest)',
              3: 'authority (offline or host)'}


def actor_role(session, actor, name='Role'):
    prop = session.field(actor, 'Actor', name)
    if prop['type'] != 'ByteProperty' or prop['size'] != 1:
        raise RuntimeError('Unsupported network role layout')
    role = session.game.read(prop['address'], 1)[0]
    if role not in ROLE_NAMES:
        raise RuntimeError('Unknown network role')
    return role


class LocalPlayerScope:
    def __init__(self, session):
        self.session = session
        session.ensure_layout('Pawn')
        self.player = session.pointer(session.pc, 'PlayerController', 'Player')
        session.remember(self.player, 'LocalPlayer')
        self.identity = session.identities[self.player]
        self.validate()

    def validate(self):
        s = self.session
        for obj in (s.pc, s.pawn):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Local controller/pawn identity changed')
        if s.identity(self.player) != self.identity or \
                s.pointer(s.pc, 'PlayerController', 'Player') != self.player or \
                not s.is_a(s.reflection.header(self.player)['class'], 'LocalPlayer'):
            raise RuntimeError('Local player ownership changed')
        if s.pointer(s.pc, 'Controller', 'Pawn') != s.pawn or \
                s.pointer(s.pawn, 'Pawn', 'Controller') != s.pc:
            raise RuntimeError('Local player possession changed')
        role = actor_role(s, s.pawn)
        if role not in (2, 3):
            raise RuntimeError('Refusing first person on a remote/unpossessed pawn')
        return role
