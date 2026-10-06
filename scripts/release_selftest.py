"""Offline packaging smoke test. Never opens or writes the game process."""
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import time
import traceback
import uuid


def signal_child(name, result):
    from helper_control import StopSignal
    signal=StopSignal(name)
    try:
        signal.set()
        result.send('child-ready')
    finally:
        signal.close()
        result.close()


class DummyRenderer:
    def __init__(self, window):pass
    def draw(self,intensity):pass
    def close(self):pass


def run(output):
    result={}
    try:
        import tkinter as tk
        from launcher import Launcher
        from launcher_settings import DEFAULTS, load_settings, save_settings
        from helper_control import StopSignal
        from damage_worker import DamageOverlayWorker
        from damage_overlay import vignette_pixels
        import avatar_profile, camera_attachment, camera_pose, controller_profile
        import controller_hold, combat_profile, forward_targeting, roll_direction, camera_discovery
        import equipment_profile, external_camera, game_memory, menu_state, silhouette_profile
        import keyboard_movement, mob_depth_profile, mouse_bindings, click_move_guard, cursor_profile
        import quick_turn
        import game_compatibility
        import local_player_scope, local_view_target, local_menu_scope, input_mapping_snapshot, coop_diagnostics, native_roll_hook, roll_target_data
        result['imports']='ok'
        hold=controller_hold.CameraHold()
        assert not hold.update(0,0)
        assert not hold.update(controller_hold.LEFT_THUMB,1)
        assert hold.update(controller_hold.LEFT_THUMB,3)
        assert not hold.update(controller_hold.LEFT_THUMB,6)
        assert len(combat_profile.aim_rotation(180,0))==24
        assert forward_targeting.in_front((0,0,0),(10,0,0),0)
        assert not forward_targeting.in_front((0,0,0),(-10,0,0),0)
        assert roll_direction.stick_yaw(0,0,-1)==-180.
        assert keyboard_movement.keyboard_axes(True,False,True,False)==(0.,0.)
        assert len(keyboard_movement.movement_vector(90.,(0.,1.)))==24
        assert keyboard_movement.controller_axes((0.,-.5))==(0.,-.5)
        assert cursor_profile.INVISIBLE==bytes((9,1))
        turn=quick_turn.QuickTurn()
        turn.update(0,0)
        turn.update(controller_hold.LEFT_THUMB,.1)
        turn.update(0,.15)
        turn.update(controller_hold.LEFT_THUMB,.25)
        assert turn.triggered
        assert turn.update(0,.6)==180.
        result['quick_turn']='ok'
        result['combat_and_controller_hold']='ok'
        from launcher_settings import helper_arguments
        comparison = helper_arguments(dict(DEFAULTS, first_person_controls=False, steer_roll=False))
        assert all(flag in comparison for flag in ('--native-combat', '--no-wasd', '--native-avatar-facing', '--native-roll-direction', '--coop-diagnostics'))
        result['coop_comparison_settings']='ok'
        assert '--native-roll-hook' not in helper_arguments(DEFAULTS)
        assert '--roll-target-data' not in helper_arguments(DEFAULTS)
        from build_info import ROLL_TARGET_DATA_AVAILABLE
        assert not ROLL_TARGET_DATA_AVAILABLE
        result['roll_target_data']='disabled after backward travel followed by game exit'
        from build_info import NATIVE_ROLL_AVAILABLE
        if NATIVE_ROLL_AVAILABLE:
            assert '--native-roll-hook' in helper_arguments(dict(DEFAULTS, native_roll_hook=True))
            result['native_roll_opt_in']='ok'
        else:
            try:
                helper_arguments(dict(DEFAULTS, native_roll_hook=True))
            except ValueError as error:
                assert 'disabled' in str(error)
            else:
                raise AssertionError('Failed native trial was not blocked')
            result['native_roll_opt_in']='disabled after failed live trial'
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk()
            root.withdraw()
            app=Launcher(root,directory=Path(directory))
            # Exercise compatibility display without disk discovery or network.
            app.compatibility_results.put({'state': 'unsupported'})
            app.poll_compatibility()
            assert 'not supported yet' in app.compatibility_status.get()
            app.compatibility_results.put({'state': 'compatible'})
            app.poll_compatibility()
            assert 'matches' in app.compatibility_status.get()
            result['compatibility_ui']='ok; offline states, no game process access'
            root.update_idletasks()
            result['ui']={'title':root.title(),'requested_size':[root.winfo_reqwidth(),root.winfo_reqheight()],
                          'settings':len(app.variables)}
            save_settings(app.settings_path,DEFAULTS)
            assert load_settings(app.settings_path)==DEFAULTS
            result['settings']='ok'
            root.destroy()
        name='Local\\DungeonsFirstPerson-Stop-'+uuid.uuid4().hex
        signal=StopSignal(name)
        receiver,sender=mp.get_context('spawn').Pipe(duplex=False)
        child=mp.get_context('spawn').Process(target=signal_child,args=(name,sender))
        child.start()
        sender.close()
        try:
            assert receiver.poll(15),'Packaged child did not start'
            assert receiver.recv()=='child-ready'
            child.join(5)
            assert child.exitcode==0 and signal.is_set()
            result['multiprocessing_and_stop_signal']='ok'
        finally:
            if child.is_alive():
                child.terminate();child.join(5)
            receiver.close();signal.close();child.close()
        worker=DamageOverlayWorker(0,renderer_factory=DummyRenderer)
        try:
            time.sleep(.6)
            worker.draw(.5)
            assert worker.process.is_alive()
            result['damage_worker']='ok'
        finally:
            worker.close()
        assert len(vignette_pixels(320,180))==320*180*4
        result['damage_bitmap']='ok'
        result['passed']=True
    except Exception:
        result['passed']=False
        result['error']=traceback.format_exc()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    if not result['passed']:
        raise SystemExit(1)
