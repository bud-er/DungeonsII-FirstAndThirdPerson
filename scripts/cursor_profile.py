"""Hide the local cursor using the game's native MouseTargeter override.

The pinned getter's only native direct caller updates the local cursor. Its
Invisible enum maps to EMouseCursor::None. No global cursor or code is changed.
"""
NATIVE_GUARDS = {
    0x5BC87B4: bytes.fromhex('80b91301000000740c0fb68112010000'),
    0x637EFC0: bytes.fromhex('0fb68160090000ffc883f808'),
    0x637F018+8*4: bytes.fromhex('0df03706'),
    0x637F00D: bytes.fromhex('33c0c3'),
    0x639BCC1: bytes.fromhex('e8eaca82ff888360090000'),
}
INVISIBLE = bytes((9,1))


class CursorProfile:
    def __init__(self,session):
        self.session,self.game=session,session.game
        self.applied,self.restore_failures=False,set()
        session.ensure_layout('MouseTargeter')
        if session.layouts['MouseTargeter']['size']!=0x140:
            raise RuntimeError('Unsupported native cursor targeter layout')
        for rva,expected in NATIVE_GUARDS.items():
            if self.game.read(self.game.base+rva,len(expected))!=expected:
                raise RuntimeError('Native cursor layout check failed')
        if self.game.u64(self.game.u64(session.pc)+479*8)!=self.game.base+0x637EFC0:
            raise RuntimeError('Native local mouse cursor getter changed')
        self.bind(session.pointer(session.pc,'GameplayPlayerController','MouseTargeter'))
        self.validate()

    def bind(self,targeter):
        s=self.session
        if not targeter:
            raise RuntimeError('Native cursor targeter is not ready')
        s.reflection.headers.pop(targeter,None)
        header=s.reflection.header(targeter)
        if not s.is_a(header['class'],'MouseTargeter') or header['outer']!=s.pc:
            raise RuntimeError('Cursor targeter is not owned by the local controller')
        identity=s.identity(targeter)
        saved=self.game.read(targeter+0x112,2)
        if saved[0]>9 or saved[1]>1:
            raise RuntimeError('Unexpected native mouse cursor override')
        if s.identity(targeter)!=identity or s.pointer(s.pc,'GameplayPlayerController','MouseTargeter')!=targeter:
            raise RuntimeError('Cursor targeter changed during discovery')
        # Optional cursor objects can be replaced by abilities. Keep their
        # serial guard private instead of making them critical camera objects.
        self.targeter,self.identity,self.address,self.saved=targeter,identity,targeter+0x112,saved

    def validate(self):
        s=self.session
        if s.identity(s.pc)!=s.identities[s.pc] or s.identity(self.targeter)!=self.identity or \
                s.pointer(s.pc,'GameplayPlayerController','MouseTargeter')!=self.targeter:
            raise RuntimeError('Cursor controller/targeter changed')
        current=self.game.read(self.address,2)
        if current[0]>9 or current[1]>1:
            raise RuntimeError('Unexpected native mouse cursor override')

    def update(self):
        s=self.session
        if s.identity(s.pc)!=s.identities[s.pc]:
            raise RuntimeError('Cursor controller changed')
        targeter=s.pointer(s.pc,'GameplayPlayerController','MouseTargeter')
        retired=getattr(s,'object_retired',None)
        changed=targeter!=self.targeter
        if not changed:
            changed=(retired is not None and retired(self.targeter,self.identity) is True) or \
                    s.identity(self.targeter)!=self.identity
        if changed:
            self.restore()
            if self.restore_failures:
                raise RuntimeError('Previous cursor override restoration failed')
            if not targeter:
                return False  # Native ability handoff; retain view and retry.
            self.bind(targeter)
            print('Native cursor targeter refreshed; first-person view retained.',flush=True)
        self.validate()
        self.applied=True
        if self.game.read(self.address,2)!=INVISIBLE:
            self.game.write(self.address,INVISIBLE)

    def restore(self):
        self.restore_failures.clear()
        if not self.applied:return
        try:
            # Restore our original targeter even after the PC selects another.
            # Never write the new targeter with the old one's saved override.
            retired=getattr(self.session,'object_retired',None)
            if retired is not None and retired(self.targeter,self.identity) is True:
                self.applied=False
                return
            if self.session.identity(self.targeter)!=self.identity:
                self.applied=False
                return
            self.session.reflection.headers.pop(self.targeter,None)
            header=self.session.reflection.header(self.targeter)
            if header['outer']!=self.session.pc or not self.session.is_a(header['class'],'MouseTargeter'):
                raise RuntimeError('Cursor targeter ownership changed during restoration')
            current=self.game.read(self.address,2)
            if current[0]>9 or current[1]>1:
                raise RuntimeError('Unexpected cursor override during restoration')
            if current!=INVISIBLE:
                # Native UI/abilities have already replaced our override.
                # Replaying the old gameplay snapshot would hide their cursor.
                self.applied=False
                return
            self.game.write(self.address,self.saved)
            if self.game.read(self.address,2)!=self.saved:
                raise RuntimeError('Cursor override restore did not persist')
            self.applied=False
        except (OSError,RuntimeError,ValueError):
            self.restore_failures.add('native cursor override')
