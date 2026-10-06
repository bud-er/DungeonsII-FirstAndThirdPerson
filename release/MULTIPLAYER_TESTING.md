# Test.10 direction validation

The test.9 guest capture confirmed backward force edits opposing repeated
forward position corrections. Test.10 removes late force edits and faces the
body during ordinary locomotion before native dodge commitment, with the camera
and arms tied to the view. Both native code hooks remain disabled.

In a clear area, hold backward then roll three times; compare one forward roll.
Confirm actual travel relative to the camera, not just the animation. Repeat
with left/right movement, keyboard R and controller R3, then release movement
and dodge. Test melee and bow charging/firing while backpedaling, artifact use,
menu open/close, and turning the camera during an active roll. Neither the force
nor the committed direction should be redirected mid-roll. A simultaneous move
and dodge can still commit the previous native body heading. Verify both co-op
roles and the friend's view. The local guest user confirmed backward rolls work
and attacks feel normal; read-only capture measured actual backward travel.
Keyboard-specific, host/observer and long-session checks remain pending.

# Retained test.9 validation

Test.8 still dropped first person while idle and logged a mob-silhouette
restoration blocker. Test.9 extends saved-slot retirement checks to enemy
actors as well as meshes, and handles optional roll-source disappearance.
Play through ordinary enemy deaths/despawns, idle periods and repeated rolls;
check F4, F6 off/on and normal helper shutdown. Keep any new failure log: it
now identifies actor/mesh details, memory address and code path.

Backward-roll direction is a separate unresolved issue. Compare held backward
movement before R/R3 with simultaneous movement+R/R3, both input modes and both
co-op roles. Do not treat camera stability or offline tests as proof that the
server accepted backward travel. Both failed native experiments stay disabled.

# Retained test.8 validation

Test.8 lets the native ranged-facing task finish before the helper resumes
camera-facing combat. Test initial stationary shots, rapid taps, charge/release,
turning, backpedaling, and switching between controller and mouse/keyboard.
Confirm actual arrows and ammo use; a bow animation alone does not prove a shot.
The camera should keep its look direction while native targeting completes.

Open and close the world map, inventory, emotes, dialogue and mini inventory.
Full menus should pause first person and resume after closing; small menus
should keep the view and hand back native UI input. Food pickups should be
checked too, but the supplied friend log did not establish food as the cause.

The old local helper logged a mob-silhouette restoration blocker after a partial
memory read. Test.8 checks saved object slots before accessing retired meshes
and preserves failures on still-live unreadable objects. Check defeated mobs,
level changes, F4, F6 off/on and normal helper shutdown. Keep any error log.

These are live acceptance checks for the new changes. Offline tests and
read-only native checks do not establish the friend's outcome.

# Co-op baseline and separate native prototype

Historical test.7 baseline, build 25647713. The friend's test.6 bow dropout log
reports "Cursor controller/targeter changed", then incomplete restoration
blocking every F6 retry. Cursor replacement now has its own guarded handoff;
the optional roll curve can retire without stopping first person. Recovery
restores first, discards stale discovery, and makes three delayed attempts.
F6 retries recovery; F5 restores only. Offline regression checks pass, but
the live result is pending. Keep both native experiments disabled.

Test bow charging/release while standing, turning and backpedaling, including
projectile artifacts and aiming down near floor loot. Confirm ammo/projectile
consumption and the friend's view. If first person drops, retain each player's
log and its "Restoration blockers" lines. Test recovery while the game stays
open. A later access-violation crash appears in the friend's test.6 log and a
local dump; its cause and backward rolling remain open.

Test.6 supports build 25647713. The user confirmed smoother keyboard/controller
turning in single player after camera pacing and incremental mesh scans changed.
The separate native target-data trial rolled backward, then exited directly to
desktop; Steam recorded exit code 0. That trial and the earlier native trial are
blocked. Backward rolling remains unresolved. Validate long-session turning and
distant co-op presentation separately with the default data-only helper.

`0.2.0-coop-test.5` supports Steam build 25647713 after the update.
The native roll trial reversed backward/forward travel and was followed by a
game exit. Native installation is now disabled; this build uses the original
data-only camera and controls. Compatibility guards and 304 offline tests
pass; controller/observer results remain pending. See [the failed prototype
record](NATIVE_ROLL_PROTOTYPE.md). Earlier stable `0.2.0-coop-test.3`
provides the historical control baseline below and requires build 25041023.
Backward roll and camera jitter remain unresolved until the live result.

Test.5 keeps first person during MiniInventory, villager dialogue and quest
acceptance while restoring native UI input. Test their menu controls, cursor,
and look/movement after closing. Full inventory/map/pause/emotes still pause
the camera. Coherent input snapshots retry transient rebuilds, and recycled
trigger headers are refreshed to address the local helper suspension that
left the game running. Visual confirmation and long-session stability remain
pending; this is separate from the earlier native roll game exit.

A live capture established that dialogue selects a quest villager as the local
camera manager's view target. The final guard keeps those targets on the
existing player camera; it awaits a live test. Other camera actors still pause
first person and restore the body. After several menus and at least a longer
play session, check whether jitter grows or the helper suspends. The local log
now includes update timing and input record counts. Preserve each player's own
log if the issue repeats; one PC's log cannot establish the other's failure.

This build contains the working first-person controls. Offline play has been
confirmed by the player. Guest bow firing while backpedaling, left-click
attacks, cursor hiding and menu restoration also passed a live co-op test.
Backward R3 rolls remain intermittent; host and observer checks are pending.
Right-stick look without dodging was reconfirmed after explicitly disabling
the native axis key. Test.3 also suppresses duplicate directional R3 rows
left in rebuilt runtime contexts. It retains the original roll-force helper;
no pre-activation roll fix or native-input experiment is included.
Your friend can begin with the normal, unmodded game. They do not need this
helper to act as the observer. Do not install the old UE4SS experiments.

## What we're checking

| Change | Intended scope | What your friend should see |
| --- | --- | --- |
| Eye-level camera, FOV, menu resume, quick turn | Your view | Their normal camera; your normal character/actions |
| Hidden head/body/armor, visible arms | Your non-replicated mesh presentation | Your complete head, body, legs and equipped armor |
| F4 white/red mob silhouettes | Render flags in your process; players excluded | Their ordinary outlines and enemy visibility |
| Damage edge flash | Your separate overlay | No added overlay on their screen |
| WASD and camera-facing combat | Your local input and combat preferences | Normal movement, attacks, arrows, pickups and resulting damage |
| Left-stick R3 direction | Direction edit on a newly started native roll force | The same roll path you experience, with native collision/cooldown |

The last two rows change gameplay actions. They are **not purely visual**.
Editing a predicted roll after it starts may disagree with the server's
roll data. A guard accepting an autonomous proxy is not proof of correct
prediction/replication. See [Epic's server movement and root-motion documentation](https://dev.epicgames.com/documentation/en-us/unreal-engine/understanding-networked-movement-in-the-character-movement-component-for-unreal-engine).

The helper selects exactly one controller owned by a LocalPlayer, confirms
the controlled pawn points back to it, and checks identities and network
role before active updates. Simulated remote pawns are refused. Body/armor
profiles are confined to that local character; the mob silhouette profile
excludes all player characters. Diagnostics only read state. These checks
reduce accidental remote-player edits; the observer test is still needed.
Host-side mesh/socket effects also need observation.

## Before enabling first person

1. Extract the new Windows ZIP. Stop an older helper with **F10** or its
   **Stop & restore** before starting this one.
2. Launch the game normally and sign in. Join your friend's session with
   the helper stopped or first person **OFF**. Confirm normal movement,
   pickups and combat work. If sign-in/join fails here, record the exact
   game error separately; this build does not change authentication.
3. Open the new launcher, leave its three co-op options enabled, and start
   the helper. It starts OFF. Your friend keeps their game unmodded.
4. Note the level, who hosts, input device, and armor/weapons. The supported
   executable is Steam build 25647713.

No terrain package is in this download or required for this test. Keep the
separate terrain experiment out of your friend's install. The development
PC's existing dirt cap is independent; note its presence in your report.

## Pass 1: your friend hosts, you join

Enable first person with F6 or a two-second L3 hold. At a safe spot, have
your friend watch your character while you look down, turn, toggle F6 twice,
and equip different armor. They should always see your complete character.
Also check that **their** full body/armor stays visible on your screen.
Press **Ctrl+F11** when first person has fully settled.

Test the following, with cooldowns between rolls:

- Walk forward, sideways and backward, including WASD if available.
- Move the right stick without pressing R3: only the camera should turn.
  Then press R3 and LB separately, including after opening/closing a menu.
- Backpedal while hitting a mob ahead; check its health/damage together.
- Fire quick and charged arrows at mobs ahead. Compare arrow direction and
  hits on both screens while moving backward too. Check that every firing
  animation releases an arrow and consumes ammunition. Pick up with A or
  Shift+left-click. Check keyboard left-click attacks without walking toward
  the cursor, and that the cursor hides only during first-person gameplay.
- Click R3 with the left stick backward and sideways. Compare actual
  start/end positions on both screens. Watch for snapping back, different
  paths, freezes or unintended extra rolls.
- Double-click L3 for the quick turn, then attack in the new view.
- Take a hit and toggle F4 near a mob behind a wall. Your friend should
  retain their normal visuals and no added edge flash.
- Open/close inventory, map, pause and LB emotes; resume without F6.

If rolls differ, stop/restore, **uncheck Steer dodge direction**, restart
the helper and repeat in the same place. This removes the force edit but
keeps camera-facing aim/arms, which can still influence native facing.
For a broader comparison, stop again and also **uncheck First-person
controls**. That disables WASD/left-stick adapters, mouse remapping, combat
steering and view-facing mesh yaw.
Body hiding/camera remain, but arms may face away from the view. Keyboard
movement becomes native click-to-move and S opens its normal menu.
Keep Record co-op test snapshots enabled throughout.

## Pass 2: you host, your friend joins

Repeat with the original settings enabled and your friend still unmodded.
This specifically checks whether presentation or socket-facing changes on
the host affect remote views or attacks. A guest pass cannot prove this.

## Session changes and cleanup

Test a level transition and, when convenient, death/revival, leaving and
rejoining. A changed pawn can suspend first person; enable it again once
the new playable area is ready. Menus should resume automatically. A new
game process reconnects OFF. Finish with **F10 / Stop & restore**, checking
normal camera/controls return, without a hidden body or stuck S key.

If the view/control state looks wrong, **F5** clears the selection and
retries restoration. Preserve the log before another launch. Restart the
game if restoration failed. Avoid force-killing an active helper.

## Record the result

Use **Open logs** in the launcher. Logs stay in
`%LOCALAPPDATA%\DungeonsFirstPerson\logs`. **Ctrl+F11** emits `COOP SNAPSHOT`
while first person is fully active. Automatic reads are ten seconds apart;
unchanged snapshots are suppressed. Retired/unready remote meshes count as
unavailable rather than being written. Logs include roles, enabled features,
mesh flags and visibility counts, without account IDs, actor names, raw
memory dumps, network calls or uploads.

Snapshots describe **your process**, not your friend's view. A healthy
remote mesh in your log cannot prove what they see. The label
`authority (offline or host)` cannot distinguish offline play from hosting.

Copy this report for each pass; optional side-by-side recordings help:

```text
Build: 0.2.0-coop-test.2 / game build:
Host: me / friend | level: | input: | armor/weapons:
Settings: first-person controls on/off | dodge steering on/off
Friend sees my complete body/armor: yes/no
I see friend's complete body/armor: yes/no
Movement/melee/bow/pickups match both screens: yes/no
Back/side R3 rolls match positions on both screens: yes/no
Snapbacks, extra rolls or unexpected damage: none / details
F4/flash/camera stay local: yes/no
Menus, level change, revival, rejoin, F10 restoration: results
Exact issue steps + snapshot/log filename:
```

Confirm both host roles with only your helper installed first. Testing both
players with helpers enabled can follow, but cannot replace that baseline.
