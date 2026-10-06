from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from unittest.mock import Mock
import queue
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from helper_control import StopSignal
from launcher_settings import DEFAULTS, helper_arguments, load_settings, save_settings, validate_settings


class CompatibilityUI(unittest.TestCase):
    def app(self):
        from launcher import Launcher
        app = Launcher.__new__(Launcher)
        app.compatibility_checker = Mock()
        app.compatibility_results = queue.Queue()
        app.compatibility_busy = False
        app.compatibility_next_check = float('inf')
        app.compatibility_button = Mock()
        app.choose_game_button = Mock()
        app.auto_game_button = Mock()
        app.compatibility_report_button = Mock()
        app.compatibility_status = Mock()
        app.compatibility_selection = None
        app.compatibility_result = None
        app.root = Mock()
        app.closing = False
        return app

    def test_check_runs_off_ui_thread_and_does_not_touch_tk_from_worker(self):
        app = self.app()
        thread_ids = []
        finished = threading.Event()
        def check():
            thread_ids.append(threading.get_ident())
            finished.wait(2)
            return {'state': 'unsupported'}
        app.compatibility_checker.check.side_effect = check
        app.check_compatibility()
        app.check_compatibility()  # Ignore repeat clicks while a check is active.
        self.assertTrue(app.compatibility_busy)
        app.compatibility_status.set.assert_called_once_with('Checking your running game or installation…')
        finished.set()
        result = app.compatibility_results.get(timeout=5)
        self.assertNotEqual(thread_ids, [threading.get_ident()])
        app.compatibility_checker.check.assert_called_once()
        app.compatibility_status.set.assert_called_once()  # Worker never uses Tk.
        app.compatibility_results.put(result)
        app.poll_compatibility()
        self.assertFalse(app.compatibility_busy)
        self.assertIn('not supported yet', app.compatibility_status.set.call_args.args[0])

    def test_check_failure_is_unknown_and_does_not_start_a_helper(self):
        app = self.app()
        app.start = Mock()
        app.compatibility_checker.check.side_effect = OSError('File being replaced')
        app.check_compatibility()
        result = app.compatibility_results.get(timeout=5)
        self.assertEqual(result['state'], 'unknown')
        app.compatibility_results.put(result)
        app.poll_compatibility()
        self.assertIn('not confirmed', app.compatibility_status.set.call_args.args[0])
        app.start.assert_not_called()

    def test_checks_repeat_after_interval_and_stop_when_closing(self):
        app = self.app()
        app.compatibility_results.put({'state': 'compatible'})
        with patch('launcher.time.monotonic', return_value=100):
            app.poll_compatibility()
        self.assertEqual(app.compatibility_next_check, 160)
        app.check_compatibility = Mock()
        with patch('launcher.time.monotonic', return_value=159):
            app.poll_compatibility()
        app.check_compatibility.assert_not_called()
        with patch('launcher.time.monotonic', return_value=160):
            app.poll_compatibility()
        app.check_compatibility.assert_called_once()
        closed = self.app()
        closed.closing = True
        closed.check_compatibility()
        closed.compatibility_checker.check.assert_not_called()

    def test_download_button_opens_only_fixed_creator_page_on_click(self):
        from launcher import DOWNLOADS_URL
        app = self.app()
        with patch('launcher.os.startfile') as browser:
            app.open_downloads()
        browser.assert_called_once_with(DOWNLOADS_URL)
        self.assertEqual(DOWNLOADS_URL, 'https://www.patreon.com/cw/BuDeR/posts')

    def test_chosen_executable_is_checked_without_launching_or_changing_helper_arguments(self):
        app = self.app()
        app.start = Mock()
        chosen = 'X:/A different store/Dungeons-WinGDK-Shipping.exe'
        with patch('tkinter.filedialog.askopenfilename', return_value=chosen):
            app.choose_game()
        app.compatibility_results.get(timeout=5)
        app.compatibility_checker.check.assert_called_once_with(selected_executable=chosen)
        self.assertEqual(app.compatibility_selection, chosen)
        app.start.assert_not_called()

    def test_cancelled_selection_and_busy_ui_do_not_change_selection(self):
        app = self.app(); app.compatibility_selection = 'original.exe'
        with patch('tkinter.filedialog.askopenfilename', return_value='') as dialog:
            app.choose_game()
            app.compatibility_busy = True
            app.choose_game(); app.auto_detect_game()
        dialog.assert_called_once()
        self.assertEqual(app.compatibility_selection, 'original.exe')
        app.compatibility_checker.check.assert_not_called()

    def test_auto_detect_clears_selection_and_new_check_disables_stale_report(self):
        app = self.app(); app.compatibility_selection = 'chosen.exe'
        app.auto_detect_game()
        app.compatibility_results.get(timeout=5)
        self.assertIsNone(app.compatibility_selection)
        app.compatibility_checker.check.assert_called_once_with()
        app.compatibility_report_button.configure.assert_called_with(state='disabled')

    def test_reports_use_sanitizer_and_save_only_when_requested(self):
        app = self.app()
        app.compatibility_result = {'state': 'unsupported', 'source': 'selected',
            'copies': [{'executable': 'C:/Users/PrivateName/game/Dungeons-WinGDK-Shipping.exe',
                        'sha256': 'a'*64, 'state': 'unsupported'}]}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'compatibility.json'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(output)):
                app.save_compatibility_report()
            report = output.read_text()
            self.assertNotIn('PrivateName', report)
            self.assertNotIn('C:/Users', report)
            self.assertIn('a'*64, report)
            self.assertIn('Nothing was uploaded', app.compatibility_status.set.call_args.args[0])
            app.compatibility_busy = True
            with patch('tkinter.filedialog.asksaveasfilename') as dialog:
                app.save_compatibility_report()
            dialog.assert_not_called()

    def test_non_json_report_target_is_rejected(self):
        app = self.app(); app.compatibility_result = {'state': 'unknown'}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'Dungeons-WinGDK-Shipping.exe'
            target.write_bytes(b'original')
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(target)), \
                    patch('tkinter.messagebox.showerror') as error:
                app.save_compatibility_report()
            error.assert_called_once()
            self.assertEqual(target.read_bytes(), b'original')


class PortableRuntime(unittest.TestCase):
    def test_isolated_source_import_works_outside_package_and_ignores_python_environment(self):
        app_path = Path(__file__).resolve().parents[1] / 'scripts/app.py'
        code = ("import json,runpy,sys; "
                "runpy.run_path(sys.argv[1],run_name='portable_bootstrap_probe'); "
                "import launcher; "
                "print(json.dumps({'launcher':launcher.__file__, 'path':sys.path, "
                "'isolated':sys.flags.isolated, 'no_site':sys.flags.no_site, "
                "'dont_write_bytecode':sys.dont_write_bytecode}))")
        with tempfile.TemporaryDirectory(prefix='Unrelated & 日本語 ') as directory:
            foreign = Path(directory)
            (foreign / 'launcher.py').write_text("raise AssertionError('Foreign launcher imported')\n")
            env = dict(os.environ, PYTHONHOME=str(foreign / 'missing Python'),
                       PYTHONPATH=str(foreign))
            completed = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(app_path)],
                                       cwd=foreign, env=env, capture_output=True, text=True,
                                       check=True, timeout=15)
            result = json.loads(completed.stdout)
            self.assertEqual(Path(result['launcher']).resolve(), app_path.with_name('launcher.py'))
            self.assertEqual(result['isolated'], 1)
            self.assertEqual(result['no_site'], 1)
            self.assertTrue(result['dont_write_bytecode'])
            self.assertNotIn(str(foreign), result['path'])

    def test_source_helper_command_preserves_isolation_and_argument_boundaries(self):
        from launcher import app_command
        with tempfile.TemporaryDirectory(prefix='Dungeons portable ') as directory:
            base = Path(directory) / 'Game & mods 日本語'
            executable = str(base / 'runtime/pythonw.exe')
            launcher = base / 'scripts/launcher.py'
            arguments = ('--helper', r'Local\Stop event', str(base / 'logs/session one.log'),
                         '--height', '55')
            with patch('launcher.sys.frozen', False, create=True), \
                    patch('launcher.sys.executable', executable), \
                    patch('launcher.__file__', str(launcher)):
                command = app_command(*arguments)
            self.assertEqual(command, [executable, '-I', '-S', '-B',
                                       str(launcher.resolve().with_name('app.py')), *arguments])

    def test_frozen_command_keeps_its_existing_dispatch(self):
        from launcher import app_command
        with patch('launcher.sys.frozen', True, create=True), \
                patch('launcher.sys.executable', r'C:\App folder\DungeonsFirstPerson.exe'):
            self.assertEqual(app_command('--self-test', 'report file.json'),
                             [r'C:\App folder\DungeonsFirstPerson.exe', '--self-test', 'report file.json'])

    def test_portable_bootstrap_adds_only_its_source_once_and_pins_tcl(self):
        import app
        with tempfile.TemporaryDirectory(prefix='Dungeons portable ') as directory:
            package = Path(directory) / 'Game & mods 日本語'
            source, runtime = package / 'scripts', package / 'runtime'
            tcl, tk = runtime / 'tcl/tcl8.6', runtime / 'tcl/tk8.6'
            for folder, name in ((tcl, 'init.tcl'), (tk, 'tk.tcl')):
                folder.mkdir(parents=True)
                (folder / name).touch()
            inherited = dict(TCL_LIBRARY='foreign tcl', TK_LIBRARY='foreign tk',
                             TCLLIBPATH='foreign packages', KEEP_ME='unchanged')
            with patch('app.sys.frozen', False, create=True), \
                    patch('app.__file__', str(source / 'app.py')), \
                    patch('app.sys.executable', str(runtime / 'pythonw.exe')), \
                    patch('app.sys.path', ['trusted stdlib']), \
                    patch.dict('app.os.environ', inherited, clear=True):
                app.configure_source_runtime()
                app.configure_source_runtime()
                self.assertEqual(app.sys.path, [str(source.resolve()), 'trusted stdlib'])
                self.assertEqual(app.os.environ['TCL_LIBRARY'], str(tcl.resolve()))
                self.assertEqual(app.os.environ['TK_LIBRARY'], str(tk.resolve()))
                self.assertNotIn('TCLLIBPATH', app.os.environ)
                self.assertEqual(app.os.environ['KEEP_ME'], 'unchanged')

    def test_bootstrap_does_not_redirect_development_or_incomplete_tcl(self):
        import app
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            source, runtime = package / 'scripts', package / 'runtime'
            tcl, tk = runtime / 'tcl/tcl8.6', runtime / 'tcl/tk8.6'
            tcl.mkdir(parents=True)
            tk.mkdir()
            (tcl / 'init.tcl').touch()
            inherited = dict(TCL_LIBRARY='developer tcl', TK_LIBRARY='developer tk',
                             TCLLIBPATH='developer packages')
            for executable, complete in ((runtime / 'pythonw.exe', False),
                                         (package / 'system/python.exe', True)):
                if complete:
                    (tk / 'tk.tcl').touch()
                with self.subTest(executable=executable, complete=complete), \
                        patch('app.sys.frozen', False, create=True), \
                        patch('app.__file__', str(source / 'app.py')), \
                        patch('app.sys.executable', str(executable)), \
                        patch('app.sys.path', []), \
                        patch.dict('app.os.environ', inherited, clear=True):
                    app.configure_source_runtime()
                    self.assertEqual(dict(app.os.environ), inherited)


class Settings(unittest.TestCase):
    def test_saved_settings_roundtrip_and_command_line(self):
        values=dict(DEFAULTS,fov=100,hide_silhouettes=False,damage_flash=False)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            save_settings(path,values)
            loaded=load_settings(path)
        self.assertEqual(loaded,values)
        args=helper_arguments(loaded)
        self.assertIn('--keep-mob-silhouettes',args)
        self.assertIn('--no-damage-flash',args)
        self.assertNotIn('--start-enabled',args)

    def test_invalid_values_are_rejected(self):
        for key,value in [('fov',120),('height',float('nan')),('sensitivity',float('inf')),
                          ('fov',True),('damage_flash','false')]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                validate_settings(dict(DEFAULTS,**{key:value}))

    def test_coop_comparison_disables_gameplay_adapters_and_mesh_facing(self):
        args=helper_arguments(dict(DEFAULTS,first_person_controls=False,steer_roll=False))
        for flag in ('--native-combat','--no-wasd','--native-avatar-facing','--native-roll-direction','--coop-diagnostics'):
            self.assertIn(flag,args)

    def test_roll_can_be_disabled_independently_of_working_aiming(self):
        args=helper_arguments(dict(DEFAULTS,steer_roll=False,coop_diagnostics=False))
        self.assertIn('--native-roll-direction',args)
        for flag in ('--native-combat','--no-wasd','--native-avatar-facing','--coop-diagnostics'):
            self.assertNotIn(flag,args)

    def test_old_settings_get_new_defaults_without_losing_preferences(self):
        values=validate_settings(dict(fov=100,hide_body=False))
        self.assertEqual(values['fov'],100);self.assertFalse(values['hide_body'])
        for key in ('first_person_controls','steer_roll','coop_diagnostics'):
            self.assertTrue(values[key])
        self.assertFalse(values['native_roll_hook'])

    def test_native_roll_hook_is_explicitly_opt_in_and_requires_gameplay_controls(self):
        self.assertNotIn('--native-roll-hook', helper_arguments(DEFAULTS))
        with self.assertRaisesRegex(ValueError, 'disabled'):
            helper_arguments(dict(DEFAULTS, native_roll_hook=True))
        with patch('launcher_settings.NATIVE_ROLL_AVAILABLE', True):
            self.assertIn('--native-roll-hook', helper_arguments(dict(DEFAULTS, native_roll_hook=True)))
        for change in ({'first_person_controls':False}, {'steer_roll':False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                helper_arguments(dict(DEFAULTS, native_roll_hook=True, **change))

    def test_failed_native_trial_is_removed_from_saved_startup_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            save_settings(path, dict(DEFAULTS, native_roll_hook=True, fov=100))
            loaded = load_settings(path)
        self.assertFalse(loaded['native_roll_hook'])
        self.assertEqual(loaded['fov'], 100)

    def test_bad_json_does_not_overwrite_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('broken!')
            with self.assertRaises(ValueError):load_settings(path)
            self.assertEqual(path.read_text(),'broken!')

    def test_graceful_stop_signal_is_shared_and_latched(self):
        name='Local\\DungeonsFirstPerson-Stop-'+uuid.uuid4().hex
        first,second=StopSignal(name),StopSignal(name)
        try:
            self.assertFalse(second.is_set())
            first.set()
            self.assertTrue(second.is_set())
            self.assertTrue(second.is_set())
        finally:
            first.close();second.close()
