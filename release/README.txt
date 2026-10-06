DUNGEONS FIRST PERSON
0.2.0-public-beta.4 / Windows x64

A free first-person camera and control helper for Minecraft Dungeons II.
Look with a mouse or controller, move relative to your view, and keep your
visible arms, weapons and existing game combat. Tested with Steam build 25647713.

Ordinary movement faces your character toward its travel direction before a
native dodge starts; the camera and visible arms keep following your view.
Attacks keep view aim, and an active roll keeps its committed direction.
Hold the intended movement direction before dodging: changing direction and
pressing dodge simultaneously can use the previous native heading. Guest co-op
testing has confirmed backward rolls and normal-feeling attacks, with backward
travel also measured in a read-only capture. Wider validation is still pending.

Bow target selection, charging, ammo and firing timing remain controlled by
the game. The native bow can complete its turn before firing without moving
your first-person camera. The supported controls do not edit committed roll
forces or target data. Both experimental native code patches remain disabled.

INSTALL AND PLAY
1. Extract the entire ZIP into a folder of your choice.
2. Keep Start Dungeons First Person.cmd and the runtime, scripts and release
   folders together. Do not run the launcher from inside the ZIP.
3. Open Start Dungeons First Person.cmd and click Start helper.
4. Launch the game normally from your usual launcher and enter a playable area.
5. Press F6, or hold L3 for two seconds, to enable first person.

Python 3.14.0 and Tcl/Tk are included; you do not need to install Python.
This package runs readable mod source with the unmodified official Python
runtime. No installer, game-directory files, save edits or DLL loader are
needed. Leave the launcher open while playing. The helper starts
OFF and reconnects OFF after a game restart. Stop the old helper with F10
before using this download; only one helper can run at a time.

REPLACEMENT PACKAGE AND DEFENDER
Public beta.1 was retired after a Microsoft Defender detection. That detection
remains unresolved; this release does not claim it was a false positive or
that the old executable was cleared. The old packed executable is not reused.

Public beta.2 and later include readable Python files and the official Python runtime;
it contains no PyInstaller executable or bootloader. The build verifies the
runtime archive hash and the Python Software Foundation signatures on the
Python interpreters and core DLL. The community mod source and CMD launcher
are not signed by the Python Software Foundation.

The release build requires explicit clean Defender scans of the complete
application before its offline test, again after build metadata is added, and
of both final ZIPs before export. An unavailable or inconclusive scan stops
the build. These checks describe those files at that time; they are not a
universal safety guarantee or a promise about future antivirus results.
Keep protection enabled. If your security software blocks this package, stop
and report the detection; do not restore the retired executable or add an
exclusion. BUILD.json records the runtime origin and build checks. A Defender
scan receipt and SHA256 checksums accompany the download.

SUPPORTED VERSION AND LAUNCHERS
Validated reference: Steam build 25647713 / Windows x64.
Only the executable fingerprint in BUILD.json is supported. The launcher that
started the game does not matter when the executable matches. A different
store edition may contain a different binary and requires separate validation.
Minecraft Launcher/Xbox and Epic editions have not been validated for play.
Recognizing their process name does not grant compatibility. Changed or unknown
executables are refused before the helper requests game memory access.

AUTOMATIC COMPATIBILITY CHECK
The launcher checks locally at startup and every minute. It checks a recognized
running game first, then an EXE you selected, then installed Steam copies.
Checking runs in the background without contacting a server. Win64 and WinGDK
shipping process names are recognized; multiple copies or inaccessible games
are reported as unconfirmed rather than replaced by another installed copy.

Choose game EXE lets you check an installation while the game is closed. Select
the ...-Shipping.exe inside Dungeons/Binaries, not the store launcher or a shortcut.
A selection lasts for this launcher session. Auto-detect clears it. A running
game always takes priority. Check again starts a fresh local check.

Save compatibility report writes a JSON file only when you request it. It
contains the mod version, executable filename, build fingerprint and check
status, without installation paths, process IDs or account details. Nothing is
uploaded automatically. A tester with another edition can share this report
to identify the build; validation may still require further investigation and
play testing. The report cannot unlock unsupported builds.

Patreon downloads opens https://www.patreon.com/cw/BuDeR/posts in your browser
when clicked. New game versions may change memory layouts, so supporting them
requires a tested mod release. This checker does not automatically repair the
mod, download a replacement, or change its supported game fingerprint. The helper
independently checks the running game before applying camera or control changes.

CONTROLS (DEFAULT FIRST-PERSON SETTINGS)
F6 / hold L3 2 s  First person on/off; release L3 between holds
Double-click L3  Smooth 180-degree quick turn, about 0.24 seconds
Mouse/right stick Look
W / A / S / D    Move relative to the view; S menu binding temporarily disabled
Left-click       Attack without click-to-move; cursor hidden during first person
Shift+left-click Normal native interaction / item pickup
Right-click      Native bow attack / charge
R3               Dodge; left stick chooses direction, centered stick goes forward
R                Keyboard dodge; held WASD chooses direction with default steering
Left bumper      Emote wheel
F4               Show/hide full through-wall white/red mob silhouettes
F7 / F8          Decrease/increase field of view
Page Down / Up   Lower/raise eye height
F9               Preview the undirected damage flash
Ctrl+F11         Co-op log snapshot while first person is fully active
F5               Immediately restore the normal view; retry restoration
F10              Restore and quit the helper

Villager dialogue, quest acceptance and the mini inventory keep first person
with native menu input. Look/movement resume when they close. Full inventory,
map, pause and emotes use normal controls/view and then resume first person.
Alt-Tab retains the view and pauses look input. F4/F6 act in the game only.
Short L3 clicks can also trigger the game's normal guidance action.

The click fix suppresses the game's empty-ground walking action after mouse
release. Shift-click retains native item interactions and approach behavior;
use WASD or the stick for ordinary movement. The guard waits for an existing
path to finish when first-person controls start, and restores on menu/toggle/exit.

LAUNCHER SETTINGS AND CO-OP
First-person controls and movement-facing dodge direction are enabled by default.
- First-person controls: WASD/left-stick movement, camera-facing combat,
  mouse attacks and view-facing arms. Bow timing and targets remain native.
  Turning this off uses native movement, aiming and mesh facing for comparison;
  arms may no longer follow the camera.
- Face movement before dodging: your character faces ordinary movement before
  the game commits a roll. Attacks retain view aim. Requires first-person
  controls; disable this to compare the game's normal forward-dodge behavior.
- Record co-op test snapshots: read-only local logs, enabled by default.
  No upload, account identifiers, raw memory dumps or network calls.
Both native roll experiments are unavailable; saved prototype opt-ins are cleared.
Stop & restore before changing settings, then start the helper again.

Camera/body/armor hiding, outlines and damage flashes are intended to affect
only your view. Movement, aiming and roll steering affect normal game actions;
they are not wholly cosmetic. Your friend should see those normal actions
and your complete character. Test that expectation with both host roles.
The helper checks LocalPlayer ownership, possession, object identities and
network role before updates; remote simulated pawns are refused.

SETTINGS AND LOGS
Defaults: FOV 115, eye height 55, mouse sensitivity 0.12, supported features on.
Saved settings: %LOCALAPPDATA%\DungeonsFirstPerson\settings.json
Logs:           %LOCALAPPDATA%\DungeonsFirstPerson\logs
Use Open logs in the launcher. Hotkey adjustments last for that helper
session; they do not replace saved startup settings.

RECOVERY / REMOVE
Transient camera failures restore first and retry fresh discovery up to three
times. F6 retries after recovery is paused. Real restoration failures still
block new writes and appear under "Restoration blockers" in the log. A stopped
game process needs a normal game launch; this recovery is for the helper while
the game remains running.
Press F5 if the view looks wrong. F10, Stop & restore or closing the launcher
restores transient settings. Wait for restoration before replacing/deleting
the extracted folder. Restarting the game resets transient changes after a
forced helper termination. No game files are installed by this release.

KNOWN LIMITS
- The click guard requires one local input owner. Additional consumers must
  be verified remote network controllers with no local player input and no
  active click-release branch. Ambiguous ownership blocks activation.
  In-game spam-click acceptance and broader co-op host validation are pending.
- Keyboard-specific dodge validation, host behavior, your friend's view,
  broader server movement correction and long-session stability remain pending.
- A direction change and dodge on the same instant can use the previous native
  heading. Hold the movement direction briefly before pressing R or R3.
- Earlier game crashes have not been explained, including crashes during play
  with both experimental patches disabled. This beta does not claim to fix them.
- The exact trigger of a reported first-person dropout after food pickup is
  still unknown. Menu, cursor and retired-object recovery have been improved,
  but that exact event has not been reproduced and verified as fixed.
- Stationary bow firing across both input types, small-menu camera retention
  and villager dialogue still need broader play testing.
Split screen is unsupported. Terrain gaps/pop-in, occasional visible mob
feet, all armor variants, death/cutscenes and every level need more testing.
The damage flash has no direction indicator. No terrain package is bundled;
the separate local dirt-cap experiment is excluded. This is a community
public beta with unsigned mod source and launcher scripts; it does not fix
account sign-in or change networking.
NATIVE_ROLL_PROTOTYPE.md records the disabled experiments and their limitations.

OPTIONAL SUPPORT
Dungeons First Person is free to download and use. Support is entirely optional
and is not required to unlock any feature.

The launcher's Support on Patreon button opens BuDeR's membership/support page
in your default browser only when you click it:
https://www.patreon.com/cw/BuDeR/membership

CREDITS / SOURCE
Unofficial community project, not affiliated with Mojang, Microsoft or the
game's developers. No game assets, binaries or asset keys are included.
Source: MIT license in LICENSE.txt. Runtime licenses: licenses/ and
THIRD_PARTY_NOTICES.txt. Matching source ZIP and SHA256 checksums accompany
the Windows download.
