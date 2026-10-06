"""Reversible local camera attachment using existing child-array storage only.

This narrow data-only operation supports a non-replicated camera, spring arm and
player root, all owned by the same pawn. It never allocates/frees native memory
or restores an old child-array snapshot over attachments added by the game.
"""
import struct


class CameraAttachment:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.applied = False
        self.pending_removal = None
        s, g = session, session.game
        s.ensure_layout('ActorComponent')
        self.parent_address = s.field(s.camera,'SceneComponent','AttachParent')['address']
        self.socket_address = s.field(s.camera,'SceneComponent','AttachSocketName')['address']
        self.child_addresses = {obj:s.field(obj,'SceneComponent','AttachChildren')['address']
                                for obj in (s.arm,s.root)}
        for obj in (s.camera,s.arm,s.root):
            replicated = s.field(obj,'ActorComponent','bReplicates')
            if g.read(replicated['address'],1)[0] & replicated['bool']['byteMask']:
                raise RuntimeError('Camera attachment requires non-replicated components')
            # Measured ActorComponent.OwnerPrivate; verified for all three live components.
            if g.u64(obj+0xA0) != s.pawn:
                raise RuntimeError('Camera attachment component has a different owner')
        self.validate()
        if g.u64(self.parent_address) != s.arm:
            raise RuntimeError('Camera is not attached to the normal spring arm')
        _, old_children = self.children(s.arm)
        new_header, new_children = self.children(s.root)
        if old_children.count(s.camera) != 1 or s.camera in new_children:
            raise RuntimeError('Unexpected camera attachment membership')
        if len(new_children) >= struct.unpack('<Qii',new_header)[2]:
            raise RuntimeError('Player attachment array has no spare capacity')

    def validate(self):
        s = self.session
        for obj in (s.camera,s.arm,s.root):
            if s.identity(obj) != s.identities[obj]:
                raise RuntimeError('Camera attachment object changed')
        if self.game.read(self.socket_address,8) != bytes(8):
            raise RuntimeError('Camera socket changed')

    def children(self, parent):
        address = self.child_addresses[parent]
        header = self.game.read(address,16)
        pointer,count,capacity = struct.unpack('<Qii',header)
        if not 0 <= count <= capacity <= 1024 or (capacity and not pointer):
            raise RuntimeError('Invalid camera attachment child array')
        data = self.game.read(pointer,count*8) if count else b''
        values = list(struct.unpack(f'<{count}Q',data))
        if len(values) != len(set(values)) or any(not p for p in values):
            raise RuntimeError('Invalid or duplicate camera attachment children')
        return header,values

    def unchanged(self, parent, header, values):
        self.validate()
        current_header,current_values = self.children(parent)
        if current_header != header or current_values != values:
            raise RuntimeError('Attachment array changed during camera handoff; retry')

    def append(self, parent):
        s,g = self.session,self.game
        header,values = self.children(parent)
        if s.camera in values:
            return
        pointer,count,capacity = struct.unpack('<Qii',header)
        if count >= capacity:
            raise RuntimeError('Camera attachment array has no spare capacity')
        self.unchanged(parent,header,values)
        # Stage in an unused slot, then publish the new count. Never change Max
        # or the allocation pointer, both of which belong to Unreal's allocator.
        g.write(pointer+count*8,struct.pack('<Q',s.camera))
        self.unchanged(parent,header,values)
        g.write(self.child_addresses[parent]+8,struct.pack('<i',count+1))
        if self.children(parent)[1] != values+[s.camera]:
            raise RuntimeError('Camera child insertion did not stick')

    def remove(self, parent):
        s,g = self.session,self.game
        header,values = self.children(parent)
        if s.camera not in values:
            return
        pointer,count,_ = struct.unpack('<Qii',header)
        index = values.index(s.camera)
        self.unchanged(parent,header,values)
        # Shift only the following entries. Preserve children added while the
        # camera was enabled and their order, including a resized allocation.
        tail = values[index+1:]
        self.pending_removal = (parent,header,values,index)
        if tail:
            g.write(pointer+index*8,struct.pack(f'<{len(tail)}Q',*tail))
        if g.read(self.child_addresses[parent],16) != header:
            raise RuntimeError('Attachment allocation changed during removal')
        g.write(self.child_addresses[parent]+8,struct.pack('<i',count-1))
        if self.children(parent)[1] != values[:index]+tail:
            raise RuntimeError('Camera child removal did not stick')
        self.pending_removal = None

    def recover_pending(self):
        if self.pending_removal is None:
            return
        self.validate()
        parent,header,values,index = self.pending_removal
        pointer,count,capacity = struct.unpack('<Qii',header)
        current = self.game.read(self.child_addresses[parent],16)
        final = values[:index]+values[index+1:]
        if current == struct.pack('<Qii',pointer,count-1,capacity):
            if self.children(parent)[1] != final:
                raise RuntimeError('Pending camera removal changed externally')
        elif current == header:
            raw = list(struct.unpack(f'<{count}Q',self.game.read(pointer,count*8)))
            intermediate = final+[values[-1]]
            if any(v not in (before,after) for v,before,after in zip(raw,values,intermediate)):
                raise RuntimeError('Pending camera removal changed externally')
            tail = values[index+1:]
            if tail:
                self.game.write(pointer+index*8,struct.pack(f'<{len(tail)}Q',*tail))
            if self.game.read(self.child_addresses[parent],16) != header:
                raise RuntimeError('Pending camera removal allocation changed')
            self.game.write(self.child_addresses[parent]+8,struct.pack('<i',count-1))
            if self.children(parent)[1] != final:
                raise RuntimeError('Pending camera removal did not recover')
        else:
            raise RuntimeError('Pending camera removal allocation changed')
        self.pending_removal = None

    def move(self, destination, source):
        self.recover_pending()
        self.validate()
        current = self.game.u64(self.parent_address)
        if current not in (source,destination):
            raise RuntimeError('Game changed the camera parent; handoff stopped')
        # Publish the destination reference before moving the parent pointer.
        # A partial failure is recoverable by inspecting current membership.
        self.append(destination)
        self.validate()
        if self.game.u64(self.parent_address) != current:
            raise RuntimeError('Camera parent changed during handoff')
        self.game.write(self.parent_address,struct.pack('<Q',destination))
        self.remove(source)
        if self.game.u64(self.parent_address) != destination:
            raise RuntimeError('Camera attachment did not stick')

    def activate(self):
        if not self.applied:
            self.applied = True  # Also clean up a partially published insertion.
            self.move(self.session.root,self.session.arm)
        else:
            self.validate()
            if self.game.u64(self.parent_address) != self.session.root:
                raise RuntimeError('Game changed the first-person camera attachment')

    def restore(self):
        if self.applied:
            self.move(self.session.arm,self.session.root)
            self.applied = False
