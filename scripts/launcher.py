"""Small desktop launcher; the camera loop and overlay run in separate processes."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid

from build_info import APP_NAME, APP_VERSION, GAME_BUILD, NATIVE_ROLL_AVAILABLE
from helper_control import StopSignal
from launcher_settings import DEFAULTS, data_directory, helper_arguments, load_settings, save_settings


SUPPORT_URL = 'https://www.patreon.com/cw/BuDeR/membership'
DOWNLOADS_URL = 'https://www.patreon.com/cw/BuDeR/posts'
COMPATIBILITY_INTERVAL = 60.


def app_command(*args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, *args]
    return [sys.executable, '-I', '-S', '-B',
            str(Path(__file__).resolve().with_name('app.py')), *args]


def run_helper(arguments):
    name, log_path, *camera_args = arguments
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'w', encoding='utf-8', buffering=1) as output:
        sys.stdout = sys.stderr = output
        signal = None
        try:
            signal = StopSignal(name)
            print(f'{APP_NAME} {APP_VERSION} | {GAME_BUILD}', flush=True)
            from external_camera import main as camera_main
            camera_main(camera_args, stop_event=signal)
            print('Helper stopped.', flush=True)
        except Exception as error:
            print(f'ERROR: {error}', flush=True)
            traceback.print_exc()
            return 1
        finally:
            if signal is not None:
                signal.close()
    return 0


class Launcher:
    def __init__(self, root, *, directory=None):
        import tkinter as tk
        from tkinter import ttk
        self.root = root
        self.directory = directory or data_directory()
        self.settings_path = self.directory / 'settings.json'
        self.process = self.signal = self.log_path = None
        self.log_offset, self.log_buffer = 0, ''
        self.closing = self.stopping = False
        self.exit_warning = False
        self.last_error = ''
        self.widgets = []
        from game_compatibility import GameCompatibilityChecker
        self.compatibility_checker = GameCompatibilityChecker()
        self.compatibility_results = queue.Queue()
        self.compatibility_busy = False
        self.compatibility_next_check = time.monotonic() + .5
        self.compatibility_selection = None
        self.compatibility_result = None
        try:
            values = load_settings(self.settings_path)
            settings_note = ''
        except (OSError, ValueError, TypeError) as error:
            values = DEFAULTS.copy()
            settings_note = f'Settings could not be read; using defaults. {error}'
        root.title(f'{APP_NAME} — {APP_VERSION}')
        root.configure(bg='#111b19')
        root.geometry('840x840')
        root.minsize(760, 620)
        icon = (Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]/'release')/'app.ico'
        if icon.exists():
            root.iconbitmap(str(icon))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#111b19', foreground='#ebf1e8')
        style.configure('TFrame', background='#111b19')
        style.configure('TLabel', background='#111b19', foreground='#ebf1e8')
        style.configure('Muted.TLabel', foreground='#b4c4b9')
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 25))
        style.configure('Sub.TLabel', font=('Segoe UI Semibold', 12), foreground='#b4eb82')
        style.configure('TCheckbutton', background='#111b19', foreground='#ebf1e8')
        style.map('TCheckbutton', background=[('active','#20342a')], foreground=[('disabled','#6e8176')])
        style.configure('TButton', padding=(16,9), background='#283e32', foreground='#ebf1e8', borderwidth=0)
        style.map('TButton', background=[('active','#3b5842')], foreground=[('disabled','#77867a')])
        style.configure('Start.TButton', font=('Segoe UI Semibold',11), background='#aee47b', foreground='#132018')
        style.map('Start.TButton', background=[('active','#c8f39f'),('disabled','#354630')])
        style.configure('TSpinbox', fieldbackground='#203028', foreground='#ffffff', arrowsize=16)
        # Scroll the whole page on small screens or with large system fonts.
        canvas=tk.Canvas(root,bg='#111b19',highlightthickness=0)
        scrollbar=ttk.Scrollbar(root,orient='vertical',command=canvas.yview)
        scrollbar.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        page = ttk.Frame(canvas, padding=26)
        page_window=canvas.create_window((0,0),window=page,anchor='nw')
        page.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(page_window,width=event.width))
        ttk.Label(page, text='DUNGEONS / A DIFFERENT PERSPECTIVE', style='Sub.TLabel').pack(anchor='w')
        heading = ttk.Frame(page)
        heading.pack(fill='x', pady=(3,4))
        support = ttk.Frame(heading)
        support.pack(side='right')
        self.support_button = ttk.Button(support, text='Support on Patreon', command=self.open_support)
        self.support_button.pack(anchor='e')
        ttk.Label(support, text='Free to use. Support is optional.', style='Muted.TLabel').pack(anchor='e', pady=(3,0))
        ttk.Label(heading, text='First Person', style='Title.TLabel').pack(side='left', anchor='n')
        ttk.Label(page, text='Step into the world. Keep your arms, weapons and existing combat.', style='Muted.TLabel').pack(anchor='w')
        ttk.Label(page, text=f'Public beta {APP_VERSION}  •  Tested reference: {GAME_BUILD}', style='Muted.TLabel').pack(anchor='w', pady=(5,17))
        self.status = tk.StringVar(value='Ready. Start the helper, then launch the game normally.')
        self.status_label = ttk.Label(page, textvariable=self.status, wraplength=740, style='Sub.TLabel')
        self.status_label.pack(anchor='w', pady=(0,12))
        actions = ttk.Frame(page)
        actions.pack(fill='x', pady=(0,17))
        self.start_button = ttk.Button(actions, text='Start helper', style='Start.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='Stop & restore', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=10)
        ttk.Button(actions, text='Open logs', command=self.open_logs).pack(side='right')
        compatibility = ttk.Frame(page)
        compatibility.pack(fill='x', pady=(0,13))
        ttk.Label(compatibility, text='Game compatibility', style='Sub.TLabel').pack(anchor='w')
        self.compatibility_status = tk.StringVar(value='Checking your running game or installation…')
        ttk.Label(compatibility, textvariable=self.compatibility_status,
                  wraplength=720, style='Muted.TLabel').pack(anchor='w', pady=(4,6))
        compatibility_actions = ttk.Frame(compatibility)
        compatibility_actions.pack(fill='x')
        self.compatibility_button = ttk.Button(compatibility_actions, text='Check again',
                                               command=self.check_compatibility)
        self.compatibility_button.pack(side='left')
        self.choose_game_button = ttk.Button(compatibility_actions, text='Choose game EXE', command=self.choose_game)
        self.choose_game_button.pack(side='left', padx=10)
        self.auto_game_button = ttk.Button(compatibility_actions, text='Auto-detect', command=self.auto_detect_game)
        self.auto_game_button.pack(side='left')
        report_actions = ttk.Frame(compatibility)
        report_actions.pack(fill='x', pady=(6,0))
        self.compatibility_report_button = ttk.Button(report_actions, text='Save compatibility report',
                                                       command=self.save_compatibility_report, state='disabled')
        self.compatibility_report_button.pack(side='left')
        ttk.Button(report_actions, text='Patreon downloads', command=self.open_downloads).pack(side='left', padx=10)
        ttk.Label(compatibility, text='Checks locally every minute. Launch from your usual launcher; a different game build may need a tested mod update.',
                  wraplength=720, style='Muted.TLabel').pack(anchor='w', pady=(5,0))
        ttk.Label(compatibility, text='The running game takes priority. For a file check, choose the …-Shipping.exe inside Dungeons/Binaries.',
                  wraplength=720, style='Muted.TLabel').pack(anchor='w', pady=(3,0))
        ttk.Label(page, text='Startup settings', style='Sub.TLabel').pack(anchor='w')
        settings = ttk.Frame(page)
        settings.pack(fill='x', pady=(8,7))
        self.variables = {}
        for column, (key, label, low, high, step) in enumerate((
            ('fov','Field of view',65,115,5), ('height','Eye height',10,120,5),
            ('sensitivity','Mouse sensitivity',.01,2,.01),
        )):
            cell = ttk.Frame(settings)
            cell.grid(row=0,column=column,sticky='w',padx=(0,30))
            ttk.Label(cell,text=label,style='Muted.TLabel').pack(anchor='w')
            variable = self.variables[key] = tk.StringVar(value=f'{values[key]:g}')
            widget = ttk.Spinbox(cell,from_=low,to=high,increment=step,textvariable=variable,width=13)
            widget.pack(anchor='w',pady=(5,0))
            self.widgets.append(widget)
        for key, label in (('hide_body','Keep arms and weapons; hide the local body and armor'),
                           ('damage_flash','Flash screen edges when taking damage'),
                           ('hide_silhouettes','Hide through-wall mob outlines (F4 toggles in game)'),
                           ('first_person_controls','First-person controls: WASD, forward aiming and view-facing arms'),
                           ('steer_roll','Face movement before dodging with WASD / left stick (attacks keep view aim)'),
                           ('native_roll_hook','Experimental native roll prototype (unplaytested; replaces late force steering)'),
                           ('coop_diagnostics','Record co-op test snapshots locally (Ctrl+F11; no uploads)')):
            variable = self.variables[key] = tk.BooleanVar(value=values[key])
            if key == 'native_roll_hook' and not NATIVE_ROLL_AVAILABLE:
                variable.set(False)
                continue
            widget = ttk.Checkbutton(page,text=label,variable=variable)
            widget.pack(anchor='w',pady=2)
            self.widgets.append(widget)
        ttk.Label(page, text='Saved when you start. In-game adjustments last until the helper closes.', style='Muted.TLabel').pack(anchor='w',pady=(5,13))
        ttk.Label(page, text='For a control comparison, stop the helper and uncheck first-person controls and dodge steering. Arms then use normal character facing. See MULTIPLAYER_TESTING.md in this download.', wraplength=740, style='Muted.TLabel').pack(anchor='w',pady=(0,13))
        ttk.Label(page,text='In-game controls',style='Sub.TLabel').pack(anchor='w')
        controls = ttk.Frame(page)
        controls.pack(fill='x',pady=(7,12))
        rows = [('F6 / hold L3 2 s','First person on / off','Mouse / right stick','Look'),
                ('W / A / S / D','Move relative to view','Left stick','Controller movement'),
                ('Left-click','Attack','Shift + left-click','Interact / pick up'),
                ('Double-click L3','Quick turn 180°','',''),
                ('F4','Mob outlines on / off','R / R3','Dodge'),
                ('F7 / F8','Field of view','Page Down / Up','Eye height'),
                ('F9','Preview damage flash','F5 / F10','Restore / restore & quit')]
        rows.append(('Ctrl+F11','Co-op log snapshot','',''))
        for row, values_row in enumerate(rows):
            for col, text in enumerate(values_row):
                ttk.Label(controls,text=text,style='Sub.TLabel' if col%2==0 else 'Muted.TLabel').grid(
                    row=row,column=col,sticky='w',padx=(0,20),pady=2)
        ttk.Label(page,text='Menus resume first person automatically. Alt-Tab keeps your view.',style='Muted.TLabel').pack(anchor='w')
        ttk.Label(page,text='Known limits: terrain gaps/pop-in, some visible mob feet. Co-op testing is ongoing.',style='Muted.TLabel').pack(anchor='w',pady=(3,10))
        self.details = tk.Text(page,height=4,bg='#0b1310',fg='#b4c4b9',relief='flat',font=('Consolas',9),wrap='word',state='disabled',padx=9,pady=6)
        self.details.pack(fill='both',expand=True)
        if settings_note:
            self.append_log(settings_note+'\n')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(150,self.poll)

    def append_log(self, text):
        self.details.configure(state='normal')
        self.details.insert('end',text)
        if int(self.details.index('end-1c').split('.')[0]) > 300:
            self.details.delete('1.0','101.0')
        self.details.see('end')
        self.details.configure(state='disabled')

    def start(self):
        from tkinter import messagebox
        if self.process is not None:
            return
        signal = None
        try:
            values = {key:variable.get() for key,variable in self.variables.items()}
            arguments = helper_arguments(values)
            save_settings(self.settings_path,values)
            name = 'Local\\DungeonsFirstPerson-Stop-'+uuid.uuid4().hex
            signal = StopSignal(name)
            logs = self.directory / 'logs'
            logs.mkdir(parents=True,exist_ok=True)
            self.log_path = logs / (time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.log')
            self.process = subprocess.Popen(app_command('--helper',name,str(self.log_path),*arguments),
                                            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            self.signal = signal
        except (OSError,ValueError,RuntimeError) as error:
            if signal is not None:
                signal.close()
            messagebox.showerror(APP_NAME,str(error),parent=self.root)
            return
        self.log_offset,self.log_buffer,self.last_error = 0,'',''
        self.stopping = self.exit_warning = False
        self.status.set('Starting helper…')
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        for widget in self.widgets:
            widget.configure(state='disabled')

    def stop(self):
        if self.signal is not None and not self.stopping:
            self.signal.set()
            self.stopping = True
            self.stop_requested = time.monotonic()
            self.status.set('Restoring the game and stopping…')
            self.stop_button.configure(state='disabled')

    def read_log(self):
        if not self.log_path or not self.log_path.exists():
            return
        with self.log_path.open('rb') as source:
            source.seek(self.log_offset)
            data = source.read(128*1024)
            self.log_offset += len(data)
        if not data:
            return
        self.log_buffer += data.decode('utf-8',errors='replace')
        lines = self.log_buffer.split('\n')
        self.log_buffer = lines.pop()
        for line in lines:
            line=line.rstrip('\r')
            self.append_log(line+'\n')
            if line.startswith(('ERROR:', 'Camera restoration incomplete:', 'Camera suspended:', 'Cannot resume here:')):
                self.last_error = line
                self.status.set(line)
            elif self.stopping:
                continue
            elif line.startswith(('Ready:', 'First person OFF.')):
                self.status.set('Connected. Press F6 or hold L3 for 2 seconds in the game.')
            elif line.startswith(('First person ON.', 'First person resumed.')):
                self.last_error = ''
                self.status.set('First person active. F6 or a 2-second L3 hold returns to the normal camera.')
            elif line.startswith('Camera recovery '):
                self.status.set('Recovering first person. Restoring changes before reconnecting to your character.')
            elif line.startswith('Automatic camera recovery paused'):
                self.last_error = line
                self.status.set('Recovery paused. Press F6 in the game to retry; Open logs shows the blocker.')
            elif line.startswith('Original camera restored and read back successfully.'):
                self.last_error = ''
            elif line.startswith('First person paused'):
                self.status.set('First person paused; it will resume when gameplay is ready.')
            elif line.startswith(('Waiting for Minecraft', 'Game closed')):
                self.status.set('Waiting for the game. Launch it normally from your usual launcher.')

    def poll(self):
        self.poll_compatibility()
        try:
            self.read_log()
        except OSError:
            pass
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                self.process = None
                self.signal.close()
                self.signal = None
                self.start_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                for widget in self.widgets:
                    widget.configure(state='normal')
                self.status.set(self.last_error or ('Stopped. You can close this window or start again.' if code==0 else 'Helper stopped with an error. See the log below.'))
                if self.closing and code==0 and not self.last_error:
                    self.root.destroy()
                    return
                self.closing = False
            elif self.stopping and time.monotonic()-self.stop_requested > 8 and not self.exit_warning:
                self.exit_warning = True
                self.status.set('Still restoring. Keep this window open; F10 also requests a safe stop.')
        self.root.after(150,self.poll)

    def check_compatibility(self):
        if self.compatibility_busy or self.closing:
            return
        self.compatibility_busy = True
        self.set_compatibility_buttons('disabled')
        self.compatibility_report_button.configure(state='disabled')
        self.compatibility_status.set('Checking your running game or installation…')
        selected = self.compatibility_selection
        # File hashing never blocks Tk or touches the running game's memory.
        # The worker communicates through a queue; only the UI thread uses Tk.
        def check():
            try:
                result = (self.compatibility_checker.check(selected_executable=selected) if selected
                          else self.compatibility_checker.check())
            except Exception:
                result = {'state': 'unknown', 'reason': 'Could not check the game.'}
            self.compatibility_results.put(result)
        threading.Thread(target=check, name='game-compatibility', daemon=True).start()

    def poll_compatibility(self):
        try:
            result = self.compatibility_results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.compatibility_busy = False
            self.compatibility_next_check = time.monotonic() + COMPATIBILITY_INTERVAL
            self.set_compatibility_buttons('normal')
            self.compatibility_result = result
            self.compatibility_report_button.configure(state='normal')
            state = result.get('state')
            messages = {
                'compatible': 'This game executable matches the tested build. The helper checks the running game again before applying the mod.',
                'unsupported': 'This game build is not supported yet. It may be a different store edition or update. Save a compatibility report to help identify it; no game changes are permitted.',
                'not_found': 'Launch Dungeons II from your usual launcher, or choose its game EXE to check it. A missing Steam installation does not mean your launcher is unsupported.',
                'unknown': 'Compatibility is not confirmed. Close duplicate game copies, wait for updates to finish, or choose the game EXE. The helper will check again before applying the mod.',
            }
            sources = {'running': 'Running game: ', 'selected': 'Selected EXE: ', 'steam': 'Steam installation: '}
            self.compatibility_status.set(sources.get(result.get('source'), '') + messages.get(state, messages['unknown']))
        if not self.compatibility_busy and time.monotonic() >= self.compatibility_next_check:
            self.check_compatibility()

    def set_compatibility_buttons(self, state):
        for button in (self.compatibility_button, self.choose_game_button, self.auto_game_button):
            button.configure(state=state)

    def choose_game(self):
        from tkinter import filedialog
        if self.compatibility_busy or self.closing:
            return
        selected = filedialog.askopenfilename(parent=self.root, title='Choose the Dungeons II game executable',
            filetypes=[('Dungeons II game', '*-Shipping.exe'), ('Executable files', '*.exe')])
        if selected:
            self.compatibility_selection = selected
            self.check_compatibility()

    def auto_detect_game(self):
        if self.compatibility_busy or self.closing:
            return
        self.compatibility_selection = None
        self.check_compatibility()

    def save_compatibility_report(self):
        from tkinter import filedialog, messagebox
        from game_compatibility import compatibility_report
        if self.compatibility_busy or self.compatibility_result is None:
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title='Save compatibility report',
            initialfile='DungeonsFirstPerson-compatibility.json', defaultextension='.json',
            filetypes=[('Compatibility report', '*.json')])
        if not selected:
            return
        if Path(selected).suffix.lower() != '.json':
            messagebox.showerror(APP_NAME, 'Choose a .json filename for the compatibility report.', parent=self.root)
            return
        try:
            report = compatibility_report(self.compatibility_result)
            Path(selected).write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        except (OSError, ValueError) as error:
            messagebox.showerror(APP_NAME, f'Could not save the compatibility report: {error}', parent=self.root)
            return
        self.compatibility_status.set('Compatibility report saved locally. It contains build details, without installation paths or account information. Nothing was uploaded.')

    def open_logs(self):
        logs=self.directory/'logs'
        logs.mkdir(parents=True,exist_ok=True)
        os.startfile(logs)

    def open_support(self):
        self.open_page(SUPPORT_URL)

    def open_downloads(self):
        self.open_page(DOWNLOADS_URL)

    def open_page(self, url):
        from tkinter import messagebox
        try:
            os.startfile(url)
        except OSError:
            messagebox.showerror(APP_NAME,
                                 'Could not open your browser. You can visit:\n\n'+url,
                                 parent=self.root)

    def close(self):
        if self.process is None:
            self.root.destroy()
        else:
            self.closing=True
            self.stop()


def main():
    args=sys.argv[1:]
    if args and args[0]=='--helper':
        raise SystemExit(run_helper(args[1:]))
    if args and args[0]=='--self-test':
        from release_selftest import run
        run(Path(args[1]))
        return
    import tkinter as tk
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
        root=tk.Tk()
        app=Launcher(root)
        if '--launch' in args:
            root.after(200,app.start)
        root.mainloop()
    except Exception:
        from tkinter import messagebox
        messagebox.showerror(APP_NAME,traceback.format_exc())
        raise
