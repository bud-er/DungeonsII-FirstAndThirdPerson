"""Entry point for the isolated portable runtime and frozen development builds."""
import os
from pathlib import Path
import sys


def configure_source_runtime():
    if getattr(sys, 'frozen', False):
        return
    # Isolated Python omits the script directory. Add only this trusted source
    # directory; spawned display workers receive this same path from the parent.
    source = Path(__file__).resolve().parent
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    runtime = source.parent / 'runtime'
    tcl, tk = runtime / 'tcl/tcl8.6', runtime / 'tcl/tk8.6'
    if (Path(sys.executable).resolve().parent == runtime.resolve() and
            (tcl / 'init.tcl').is_file() and (tk / 'tk.tcl').is_file()):
        # Python's -I flag does not isolate Tcl's environment variables.
        os.environ.pop('TCLLIBPATH', None)
        os.environ['TCL_LIBRARY'] = str(tcl)
        os.environ['TK_LIBRARY'] = str(tk)


configure_source_runtime()

import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from launcher import main
    main()
