"""Small read-only co-op snapshots, written only to the helper's local log.

These show this process's state, never what the friend actually sees. There
are no network calls, raw memory dumps, account identifiers or actor names.
"""
import json
import struct
import time

from build_info import APP_VERSION
from local_player_scope import ROLE_NAMES, actor_role


def boolean(session, obj, cls, name):
    prop = session.field(obj, cls, name)
    if prop['type'] != 'BoolProperty' or prop['size'] != 1:
        raise RuntimeError('Unexpected presentation flag')
    return bool(session.game.read(prop['address'], 1)[0] & prop['bool']['byteMask'])


def player_presentation(session, actor, expected):
    s = session
    if s.identity(actor) != expected:
        raise RuntimeError('Player retired during snapshot')
    mesh = s.pointer(actor, 'Character', 'Mesh')
    mesh_identity = s.identity(mesh)
    result = dict(local=actor == s.pawn, role=ROLE_NAMES[actor_role(s, actor)],
                  remote_role=ROLE_NAMES[actor_role(s, actor, 'RemoteRole')],
                  mesh_hidden=boolean(s, mesh, 'SceneComponent', 'bHiddenInGame'),
                  mesh_replicated=boolean(s, mesh, 'ActorComponent', 'bReplicates'))
    asset = s.pointer(mesh, 'SkinnedMeshComponent', 'SkinnedAsset')
    if s.reflection.header(asset)['name'] == 'SK_Player_Master':
        hidden_counts = []
        for offset in (0x7A0, 0x7B0):
            descriptor = s.game.read(mesh+offset, 16)
            pointer, count, capacity = struct.unpack('<Qii', descriptor)
            if not pointer or count != 32 or not count <= capacity <= 256:
                raise RuntimeError('Visibility buffer is not ready')
            values = s.game.read(pointer, count)
            if any(v > 2 for v in values) or s.game.read(mesh+offset, 16) != descriptor:
                raise RuntimeError('Visibility buffer changed during snapshot')
            hidden_counts.append(sum(v != 1 for v in values))
        result['hidden_bones_per_buffer'] = hidden_counts
    if s.identity(actor) != expected or s.identity(mesh) != mesh_identity or \
            s.pointer(actor, 'Character', 'Mesh') != mesh:
        raise RuntimeError('Player changed during snapshot')
    return result


class CoopDiagnostics:
    def __init__(self, args):
        self.next_read, self.last = 0., None
        self.frame_count, self.frame_total, self.frame_max = 0, 0., 0.
        self.profile_times = {}
        self.features = dict(first_person_controls=not getattr(args, 'native_combat', False),
                             wasd=not getattr(args, 'no_wasd', False),
                             pending_controller_movement=not getattr(args, 'no_wasd', False),
                             native_bow_targets=not getattr(args, 'filter_ranged_targets', False) or
                                                getattr(args, 'native_ranged_targets', False),
                             roll_steering=not getattr(args, 'native_combat', False) and
                                           not getattr(args, 'native_roll_direction', False) and
                                           not getattr(args, 'roll_target_data', False) and
                                           not getattr(args, 'native_roll_hook', False),
                             native_roll_hook=getattr(args, 'native_roll_hook', False),
                             roll_target_data=getattr(args, 'roll_target_data', False),
                             sprint=getattr(args, 'sprint', False),
                             view_facing_arms=not getattr(args, 'no_avatar', False) and
                                              not getattr(args, 'native_avatar_facing', False))

    def observe_frame(self, seconds):
        self.frame_count += 1
        self.frame_total += seconds
        self.frame_max = max(self.frame_max, seconds)

    def track(self, session, scene):
        for name in ('pose', 'controller_profile', 'keyboard_movement', 'mouse_bindings',
                     'cursor_profile', 'combat_profile', 'avatar_profile', 'silhouette_profile',
                     'roll_target_profile', 'view_target_profile', 'damage_feedback', 'sprint_profile'):
            profile = getattr(session, name, None)
            if profile is None or getattr(profile, '_timing_tracked', False):
                continue
            operation = profile.update
            def measured(*args, _operation=operation, _name=name, **kwargs):
                start = time.perf_counter()
                try:
                    return _operation(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter()-start
                    row = self.profile_times.setdefault(_name, [0, 0., 0.])
                    row[0] += 1; row[1] += elapsed; row[2] = max(row[2], elapsed)
            profile.update = measured
            profile._timing_tracked = True

    def runtime_health(self, session):
        result = dict(frames=self.frame_count,
                      average_work_ms=round(1000*self.frame_total/max(1,self.frame_count), 3),
                      maximum_work_ms=round(1000*self.frame_max, 3))
        combat = getattr(session,'combat_profile',None)
        result['roll_steering_active'] = combat is not None and getattr(combat,'movement_facing_enabled',False) is True
        result['body_facing_mode'] = getattr(combat,'facing_mode','unavailable') if combat is not None else 'unavailable'
        result['late_roll_force_edit'] = False
        movement = getattr(session, 'keyboard_movement', None)
        pulse = getattr(movement, 'pulse', None)
        if pulse is not None:
            result['movement_pulse'] = pulse.diagnostics()
        sprint = getattr(session, 'sprint_profile', None)
        if sprint is not None:
            result['sprint'] = sprint.diagnostics()
        result['ranged_facing_handoff'] = combat is not None and getattr(combat,'ranged_handoff_active',False) is True
        result['profile_work_ms'] = {name: dict(average=round(total/count*1000,3),
                                               maximum=round(peak*1000,3))
                                     for name,(count,total,peak) in self.profile_times.items()}
        self.profile_times.clear()
        for profile_name, collections in (
                ('controller_profile', ('swaps','axis_blocks','thresholds')),
                ('keyboard_movement', ('bindings',)), ('mouse_bindings', ('records',)),
                ('silhouette_profile', ('records',))):
            profile = getattr(session, profile_name, None)
            if profile is not None:
                for name in collections:
                    result[profile_name+'.'+name] = len(getattr(profile, name, {}))
        self.frame_count, self.frame_total, self.frame_max = 0, 0., 0.
        return result

    def update(self, session, scene, *, forced=False, now=None):
        now = time.monotonic() if now is None else now
        if not forced and now < self.next_read:
            return
        self.next_read = now+10
        # Errors here only affect logging, never camera eligibility/restoration.
        try:
            role = session.local_scope.validate()
            for name in ('Character', 'SkinnedMeshComponent', 'ActorComponent'):
                session.ensure_layout(name)
            players, unavailable = [], 0
            catalogue = dict(getattr(scene, 'players', {}))
            catalogue[session.pawn] = session.identities[session.pawn]
            for actor, identity in catalogue.items():
                try:
                    players.append(player_presentation(session, actor, identity))
                except (OSError, RuntimeError, ValueError, KeyError, StopIteration):
                    unavailable += 1
            players.sort(key=lambda p: (not p['local'], json.dumps(p, sort_keys=True)))
            result = dict(version=APP_VERSION, local_scope='validated',
                          local_role=ROLE_NAMES[role], features=self.features,
                          players=players, unavailable_players=unavailable)
            if self.frame_count:
                result['runtime_health'] = self.runtime_health(session)
            manager = vars(session.game).get('native_roll_hook')
            if manager is not None:
                result['native_roll'] = dict(installed=manager.installed, active=manager.active,
                                            counters=list(manager.counters()))
            target = vars(session.game).get('roll_target_data_hook')
            if target is not None:
                result['roll_target_data'] = dict(installed=target.installed, active=target.active,
                                                counters=list(target.counters()))
            encoded = json.dumps(result, sort_keys=True)
            if forced or encoded != self.last:
                print('COOP SNAPSHOT '+encoded, flush=True)
                self.last = encoded
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as error:
            print(f'Co-op snapshot unavailable: {error}', flush=True)
