"""Build and verify a standalone Windows release from an explicit source allowlist."""
import hashlib
from pathlib import Path
import struct
import zipfile


ROOT=Path(__file__).resolve().parents[1]
RUNTIME=('app launcher launcher_settings game_compatibility helper_control build_info external_camera '
         'game_memory inspect_camera camera_pose camera_attachment camera_transition '
         'avatar_profile equipment_profile controller_profile controller_hold quick_turn combat_profile native_ranged_facing native_combat_activity locomotion_facing forward_targeting roll_direction camera_discovery camera_recovery damage_feedback '
         'damage_worker damage_overlay menu_state silhouette_profile mob_depth_profile keyboard_movement mouse_bindings click_move_guard cursor_profile '
         'local_player_scope local_view_target local_menu_scope coop_diagnostics native_roll_hook roll_target_data input_mapping_snapshot release_selftest').split()


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def make_icon(path):
    size=64
    pixels=bytearray()
    for y in reversed(range(size)):
        for x in range(size):
            rgb=(17,27,25)
            if 8<=x<56 and 8<=y<56:rgb=(57,88,52)
            if 13<=x<51 and 13<=y<51:rgb=(174,228,123)
            if 19<=x<45 and 23<=y<41:rgb=(17,27,25)
            if 27<=x<37 and 27<=y<37:rgb=(235,241,232)
            if 10<=x<17 and 10<=y<17:rgb=(218,246,175)
            pixels.extend((*reversed(rgb),255))
    bitmap=struct.pack('<IiiHHIIiiII',40,size,size*2,1,32,0,len(pixels),0,0,0,0)+pixels+bytes(size*size//8)
    path.write_bytes(struct.pack('<HHH',0,1,1)+struct.pack('<BBBBHHII',size,size,0,0,1,32,len(bitmap),22)+bitmap)


def archive(folder,destination):
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as package:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                package.write(path,folder.name+'/'+path.relative_to(folder).as_posix())


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from portable_build import main as build_portable
    return build_portable()


if __name__ == '__main__':
    main()
