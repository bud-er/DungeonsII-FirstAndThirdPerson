Dungeons II — First & Third Person
0.3.0-preview.11 / Windows x64

A free first/third-person camera and control helper for Minecraft Dungeons II.
Look with a mouse or controller, move relative to your view, and keep your
visible arms, weapons and existing game combat. Supported builds: Steam 25647713
and Xbox/Minecraft Launcher/Microsoft Store GDK 1.1.1.0. Epic is unverified;
unknown game builds remain blocked.

Preview11 updates the name, package filenames and documentation. Gameplay is
unchanged from Preview10. Existing preferences and logs stay in the original
DungeonsFirstPerson data folder so upgrading keeps your settings.

NEW FEATURE PREVIEW
Preview10 adds an inventory armor shortcut: highlight an equipped helmet,
chestplate, pants or boots and tap Y / Triangle to hide or show just that piece.
All four slots have been confirmed working in the local Xbox test. Choices
last until the Helper closes and stay selected across camera modes and menus.
The Helper has an option to disable this shortcut. No equipment stats or save
data are changed. First-person camera and arm angles now refresh together
between slower input/scene checks, with controller time integrated only once.
The local Xbox tester confirmed the recurring turn snap is gone and walking
remains normal with mouse/controller testing. Broader sessions remain untested.

Preview9 brings first-person arm rotation alongside the camera update instead
of delaying it until after slower input and scene checks. This targets the arm
jitter seen when turning with either mouse or controller. Local walking has
been confirmed improved with the separate movement process retained below;
Preview10 subsequently passed the local arm-turning test. Broader sessions remain untested.

Preview8 changes camera-relative movement delivery to address walking-animation
flicker and stop-start movement in custom views. A separate, bounded input worker
supplies the game's consumed movement vector independently of slower scene checks.
It reads physical controls afresh and stops for focus loss, native movement locks,
menus, expired camera updates and changed player/mapping ownership. Normal camera
exit joins the worker before restoring input. No movement speed, animation, physics,
game files or executable instructions are patched by this change.
This is a private test build; do not upload it as the next public release yet.

The Helper includes a separate preview profile for the Xbox/Minecraft Launcher/
Microsoft Store GDK 1.1.1.0 edition. Start that edition normally, enter a playable
area, then start the Helper. The protected executable is checked through a
read-only process handle before the Helper requests access to change anything.
Camera, controller, mouse, menu, avatar and sprint setup passed read-only checks.
The local Xbox walking test passed; complete gameplay acceptance is still pending.

ARMOR VISIBILITY
Startup settings now include Hide helmet, Hide chestplate, Hide pants and Hide
boots, plus Hide all armor / Show all armor. All are visible by default.
With the inventory shortcut enabled, these choices also apply in normal view.
Chest includes its sleeves; pants include their waist pieces. Gear stays equipped
with its existing stats. Stop the Helper before changing startup settings.
F5 resets visibility and suspends the shortcut until the next F6 activation;
F10/Helper shutdown restores the prior native visibility. Other players
still see normal equipment. This does not include swapping an item's design.

Preview6's right-stick axis suppression and R3 dodge fixes are retained for both
perspectives. Controller gameplay retesting is still needed; external polling
cannot prevent every event between Helper updates.

The previous revision changed sprint to use the game's walking-speed calculation
and added brief directional-dodge priority while attacking in third person. The
tester reported occasional sprint jitter even in solo play; the old speed
output was repeatedly being replaced by the game. These fixes need a retest.
The smooth camera, shop handling, airborne-click route and manual F2 shoulder
switch from earlier previews remain.
Enter first person with F6 / hold L3, then use F3 / triple-click L3 to switch
first/third. R3 still dodges immediately. Adjust shoulder distance, left/right
offset, height, FOV and transition duration in the Helper. Third person shows
your full character and faces movement independently of the camera; attacking
uses the camera heading. Keep movement-facing controls enabled for this behavior.
Use F2 in third person to slide to the opposite shoulder. The selected side
survives menus and camera toggles until the Helper stops; the next Helper uses
the saved left/right offset. Disable shoulder swapping in the Helper if desired.
A zero side offset stays centered. There is no automatic wall switch or world
collision probe yet; use F2 manually if one side is obstructed. Walls can still
clip the view. F2 does nothing in menus, first person or another application.

Sprint is OFF by default. Enable it in the Helper to try single-click L3,
tap Shift or double-tap/flick a movement direction. The default boost is 20%,
adjustable from 5% to 35%. It affects walking only and ends when you stop,
attack, dodge, leave the ground, open a menu or switch away from the game.
The Helper now changes one native walking multiplier and lets the game compute
speed. Native speed buffs continue to apply. Native walking-state changes can
end sprint. If the game repeatedly replaces the multiplier, sprint pauses for
this Helper session and a notice explains why. Camera controls and ordinary
movement remain available. Optional sprint validation failures also leave the
camera usable. Settings are preserved. Actual smoothness and network acceptance
still need testing; no network correction behavior is overridden.
Double L3 / double Shift makes a smooth quick turn; holding L3 for two seconds
toggles back to normal view. Single/double L3 wait briefly for another click.
Shift-click interaction consumes the Shift gesture. Short L3 clicks may also
trigger native guidance. These features can be disabled independently.

Ordinary movement faces your character toward its travel direction before a
native dodge starts; the camera and visible arms keep following your view.
In third person, a fresh directional R/R3 press briefly gets facing priority
over attacking. Native attack locks, ranged-turn handoffs and already-started
rolls retain control. Otherwise attacks use view aim as before.
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
2. Keep Start Dungeons II Camera Mod.cmd and the runtime, scripts and release
   folders together. Do not run the launcher from inside the ZIP.
3. Open Start Dungeons II Camera Mod.cmd.
4. Launch the game normally from your usual launcher, enter a playable area,
   then click Start helper. Start the protected Xbox edition before attaching.
5. Press F6, or hold L3 for two seconds, to enable first person.

Python 3.14.0 and Tcl/Tk are included; you do not need to install Python.
This package runs readable mod source with an application runtime from official
Python. Unused pip/ensurepip installers are omitted; retained runtime files
are unchanged. No installer, game-directory files, save edits or DLL loader are
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

Public beta.5 omits pip, its metadata and ensurepip, including unused distlib
launchers and the nested pip wheel. The loader only uses the standard library.
RUNTIME_OMISSIONS.json lists the upstream hashes of all omitted files. The
Python interpreters, core DLLs, Tcl/Tk and other retained runtime files keep
their official bytes. This preview retains that packaging and adds the camera
and sprint features described above.

The reported beta.4 VirusTotal result showed one detection for pip's w32.exe,
whose bytes match the pinned official Python archive. This is provenance
evidence, not a vendor false-positive ruling or Nexus approval. Nexus may still
require review of the Python executables. No scan bypass is claimed.

SUPPORTED VERSION AND LAUNCHERS
Steam reference: build 25647713 / Windows x64. Exact file fingerprint or strict
certificate-only equivalence is required; all other file bytes must match.
Xbox/Minecraft Launcher/Microsoft Store preview: GDK 1.1.1.0, measured separately.
Windows may refuse direct file reads of this edition even with ordinary read
permissions. Start it through its usual launcher; do not change folder ownership.
The Helper checks a pinned mapped gameplay/engine code range, the full unwind
table, PE layout and native guards using read-only access before requesting
write access. The runtime-changing tail is excluded from the code fingerprint;
this mapped check is explicitly different from a full-file antivirus/file hash.
No game file or executable instruction is changed. No DRM or launcher is bypassed.
A different or updated build remains blocked. No Epic build has been validated.
Xbox setup, local walking, first-person arm turning, all four inventory armor
toggles, menu mouse control and armor retention across views have passed local
testing. Broader gameplay and other GDK versions have not been validated.

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

Save compatibility report lets you choose a JSON file. Copy support details
puts the same information on your clipboard. It contains the mod version,
executable filename, build fingerprint, bounded PE header identifiers and a
specific DFP status code, without installation paths, process IDs, account
details or raw error strings. The latest running/selected game report is also
saved locally as logs/compatibility-latest.json (Open logs). Closing the game
does not erase that last useful report. Nothing is uploaded.
A tester with another edition can paste the copied details into their comment
to identify the build; validation may still require further investigation and
play testing. The report cannot unlock unsupported builds.

Patreon downloads opens https://www.patreon.com/cw/BuDeR/posts in your browser
when clicked. New game versions may change memory layouts, so supporting them
requires a tested mod release. This checker does not automatically repair the
mod or download a replacement. The helper
independently checks the running game before applying camera or control changes.

CONTROLS (FIRST / THIRD PERSON)
F6 / hold L3 2 s  First person on/off; release L3 between holds
F3 / triple L3   Switch first/third while the custom camera is on
F2              Smoothly swap left/right shoulder while in third person
Single L3/Shift  Start optional sprint; off by default
Double L3/Shift  Smooth 180-degree quick turn, about 0.24 seconds
Double direction Optional sprint via WASD double-tap / stick double-flick
Mouse/right stick Look
W / A / S / D    Move relative to the view; S menu binding temporarily disabled
Left-click       Attack without click-to-move; cursor hidden during first person
Airborne click   Native heavy-jump attack; release LMB before the airborne click
Shift+left-click Normal native interaction / item pickup
Right-click      Native bow attack / charge
R3               Dodge; left stick chooses direction, centered stick goes forward
R                Keyboard dodge; held WASD chooses direction with default steering
Left bumper      Emote wheel
F4               Show/hide full through-wall white/red mob silhouettes
F7 / F8          Decrease/increase field of view
Page Down / Up   Lower/raise first-person eye height or third-person shoulder
F9               Preview the undirected damage flash
Ctrl+F11         Co-op log snapshot while first person is fully active
F5               Immediately restore the normal view; retry restoration
F10              Restore and quit the helper

Villager dialogue, quest acceptance, Enchantsmith, merchant shops, purchase previews and the
mini inventory keep your selected first/third view with native menu input.
Look/movement resume when they close, without a camera re-entry fade. Full inventory,
map, pause and emotes use normal controls/view and then resume your camera mode.
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
Defaults: FOV 115, eye height 55, mouse sensitivity 0.12; sprint off.
Shoulder defaults: distance 220, side 45, height 65, FOV 90; transition 0.45 s.
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
- Local shoulder movement and camera/armor transitions have passed play tests.
  Shoulder wall clipping is possible. Sprint smoothness, co-op sprint, attack
  dodges, right-stick roll suppression and new gestures need broader testing.
- Airborne LMB now routes to the existing native heavy-jump attack, but the
  reported down-slam problem still needs live confirmation. Q/side-button
  shortcuts remain intact; native ability checks still decide activation.
  Unsupported airborne-action metadata disables only that extra routing and
  logs the reason; ground mouse controls remain available.
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
Dungeons II — First & Third Person is free to download and use. Support is entirely optional
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
