# Build the Windows portable release

Use 64-bit Windows and Python 3.14. The builder uses only the standard library;
no pip dependencies or PyInstaller are required. Microsoft Defender must be
available with antivirus and real-time protection enabled in Normal mode.
The scan parser currently requires its supported English completion output;
unknown or localized output is treated as unconfirmed and stops the build.

The bundled runtime is the official Python 3.14.0 AMD64 distribution with
Tcl/Tk. Its URL, version and SHA256 are pinned in `release/python-runtime.json`.
Download a fresh copy into the private cache, then build from the source folder:

```powershell
New-Item -ItemType Directory -Force .cache/runtime-public-beta2 | Out-Null
$runtimeLock = Get-Content release/python-runtime.json -Raw | ConvertFrom-Json
Invoke-WebRequest -Uri $runtimeLock.url -OutFile .cache/runtime-public-beta2/python-3.14.0-amd64.zip
python -m unittest discover -s tests -p 'test_*.py'
python scripts/build_release.py --runtime-archive .cache/runtime-public-beta2/python-3.14.0-amd64.zip
```

The builder checks the archive against the pinned hash, validates extraction
paths and required runtime files, and verifies the Python Software Foundation
Authenticode signatures on `python.exe`, `pythonw.exe` and `python314.dll`.
Runtime files retain their official bytes. The application contains the explicit
source allowlist, its icon, documentation and actual Python, Tcl, Tk
and OpenSSL license texts. Original notices for other bundled runtime components
remain inside the runtime tree. Both dormant native roll experiments are
included as readable source and remain blocked by the release settings.

The entrypoint is `Start Dungeons First Person.cmd`. Keep that file and the
`runtime`, `scripts` and `release` folders together. Python starts with `-I -S -B`;
the bootstrap adds its own resolved source directory and selects the bundled
Tcl/Tk libraries. Helper subprocesses retain these flags. End users do not need
a Python installation or a game-directory loader.

The build requires three Defender scan gates:

1. Scan the complete application before running its bundled Python.
2. Run the offline smoke tests, add build metadata and file hashes, then scan
   the completed application again.
3. Create the Windows and source ZIPs and scan both before exporting them.

Each gate requires an explicit completed, threat-free result and unchanged
file contents. A detection, timeout, missing protection, unknown scan output or
changed file stops the build. The builder does not change Defender settings,
create exclusions, restore quarantined items or reuse public beta.1's executable.
A successful scan describes those exact files, signatures and time; it is not
a universal safety guarantee. Public beta.1 was retired after a Defender
detection that remains unresolved. No false-positive clearance is claimed.

The offline tests run under both packaged `python.exe` and `pythonw.exe` from
an unrelated working directory with conflicting Python/Tcl environment paths.
They cover Tk launcher construction, multiprocessing, the dummy damage worker,
saved settings and cross-process graceful-stop signaling. They never open or
write the game process and do not start gameplay. Successful builds export
ZIPs, SHA256 checksums and a public Defender scan receipt to `dist/`;
`dist/latest-build.json` identifies those artifacts and detailed scan reports
retained in the private build directory. `BUILD.json`
records runtime provenance, source hashes and smoke-test results, while
`FILES.sha256` lists the application files. The source ZIP includes matching
runtime modules, tests, documentation and the build recipe, but no runtime cache,
personal logs, memory captures or historical game-directory loaders.

Development launch: `python -I -S -B scripts/app.py`. Runtime code uses the
standard library and Windows APIs. The optional Support on Patreon button opens
the creator's page in the default browser when clicked. The launcher checks the
running game's compatibility locally in the background, with a selected EXE
or installed Steam copies as fallbacks, and links to
the creator's Patreon downloads on request. It does not automatically repair
unknown builds, download code or make network requests. The helper's independent
executable SHA256 guard remains mandatory before memory access. Another
launcher is accepted when the running executable matches; a different store
binary requires separate validation. Local compatibility reports omit paths,
process IDs and account details. The source archive also includes the draft
Nexus description and author upload notes; no Nexus upload is performed.

Optional development-only Unicorn instruction emulation runs when Unicorn is
installed; otherwise those tests are explicitly skipped. It is not bundled
with the app or required for supported builds. See `NATIVE_ROLL_PROTOTYPE.md`
for the offline/live validation distinction. Gameplay remains based on test.10:
guest backward rolls and attacks were confirmed, while keyboard-specific,
host/observer and long-session validation, earlier crashes and the exact
food-associated dropout remain unresolved.
