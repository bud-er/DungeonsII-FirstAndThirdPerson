# 0.2.0-public-beta.4

- Check the running game independently of its launcher, before falling back to
  a manually selected game executable or installed Steam copies. Detect Win64
  and WinGDK process names, including inaccessible or duplicate candidates,
  instead of silently waiting for Steam's process name alone.
- Add Choose game EXE, Auto-detect and Save compatibility report controls.
  Export reviewed build identifiers locally without installation paths,
  process IDs or account details. No automatic upload or game launch occurs.
- Keep the exact tested executable fingerprint mandatory. Identifying a store
  edition does not make a different binary compatible. Minecraft Launcher/Xbox
  and Epic editions remain unvalidated; only the Steam reference build has
  been tested in play.
- Validate through a query-only process handle before requesting memory access;
  recheck process creation identity and path before attachment.
- Include a Nexus Mods description draft and author upload notes in the source
  archive. Keep the beta.3 gameplay changes and their stated testing limits.

# 0.2.0-public-beta.3

- Suppress the native empty-ground click-release walking branch during first
  person. The force-attack alias alone ended with the mouse button and allowed
  this branch through during repeated clicks. Preserve separate native melee
  and interaction branches, and restore the selection flag on menu/toggle/exit.
  Require one verified local input owner and reject ambiguous additional
  consumers. Recognize remote network controllers with no local player input.
  Wait for an existing path to finish before enabling the new guard. No
  executable code is patched. In-game click acceptance is still pending.
- Detect menus through the local player's currently displayed UI, including
  inventory when the native controller UI-capture flag remains false. Check
  ownership, visible parents, the displayed stack entry and live viewport
  membership so stale menu objects cannot keep the helper paused.
- Release mouse look and gameplay adapters for UI input. Preserve cursor
  settings already selected by a menu instead of replaying a gameplay snapshot.
- Add a background local Steam compatibility check at startup and every minute,
  with a manual check button and Patreon downloads link. Unknown game versions
  still require a tested mod release; no automatic repair or download is claimed.
  The helper's independent full executable hash check remains mandatory.
- Retain the official Python runtime packaging and Defender scan gates.

# 0.2.0-public-beta.2

- Replace the packed application with readable mod source and an unmodified
  official Python 3.14.0/Tcl/Tk runtime. Open `Start Dungeons First Person.cmd`;
  no separate Python installation is required. Keep the full extracted folder.
- Pin the official archive URL and SHA256 in `release/python-runtime.json` and
  verify Python Software Foundation interpreter/core DLL signatures. Use only
  the standard library to build; PyInstaller is no longer included or required.
- Preserve interpreter isolation for the launcher, helper and spawned damage
  worker, and select the bundled Tcl/Tk libraries when using portable Python.
  Test startup from an unrelated directory with conflicting environment paths.
- Require explicit clean Defender scans of the whole app before execution,
  after metadata is added, and of both final ZIPs before export. Failed,
  unavailable or inconclusive scans block release. Results apply to the scanned
  files and time, not every security product or future antivirus result.
- Retire public beta.1 after its Defender detection. The detection remains
  unresolved; no false-positive finding or clearance of its executable is claimed.
- Preserve test.10 gameplay and controls, including confirmed guest backward
  rolls and normal-feeling attacks. Keyboard, host/observer and long-session
  validation remain pending; earlier crashes and the exact food event remain
  unresolved. Both native roll experiments remain disabled.

# 0.2.0-public-beta.1

- Add a Support on Patreon button that opens BuDeR's membership page in the
  default browser. The mod is free to use and support is optional. The button
  remains available while the helper runs; no account or payment is required.
- Package the working test.10 gameplay code as a public beta and simplify the
  release instructions for new players. Existing compatibility and co-op
  testing limits still apply.

# 0.2.0-coop-test.10

- Replace late roll-force changes with body facing during ordinary movement,
  before the game commits its native dodge direction. The camera and visible
  arms remain tied to the view. Native force, target data and timing are untouched.
- Give held attacks and registered melee/ranged/artifact abilities view-facing
  priority. Preserve the native ranged turn and already-active roll direction;
  movement locks, lost focus or unknown ability state defer movement-facing.
- Read only the local pawn's registered ability specs and owned instances, with
  serial, class, layout, ownership and coherent-array checks. Optional rebuilds
  preserve first person. No native hook or instruction patch is enabled.
- The test.9 guest reproducer showed four backward attempts ending 21–71 units
  forward while forward controls travelled about 911–915 units. The old force
  reversal occurred after native commitment and fought repeated corrections.
  The local guest user confirmed backward rolls work and attacks feel normal;
  a read-only trial captured native backward force and actual backward travel.
  Keyboard, host/observer and longer sessions still need acceptance. Simultaneous
  move+dodge can use the last native heading; hold direction before dodging.
- Retain test.9 actor-retirement recovery and test.8 bow/menu fixes. The earlier
  game crash and the friend's exact food-associated event remain unresolved.

# 0.2.0-coop-test.9

- Check saved object slots before reading enemy actor silhouette flags, covering
  the actor path missed by test.8's mesh-only fix. Restore live records before
  discarding them; unreadable live records remain recoverable failures.
- Isolate optional roll-source retirement/reallocation from the camera. Refuse
  stale source writes and preserve local movement/possession guards.
- Log the failed memory request and a short code path, with separate actor and
  mesh restoration detail, so a later dropout identifies its actual origin.
- Keep the test.8 bow and menu changes. Backward-roll direction, the historical
  game crash and the friend's exact food event remain unconfirmed/unresolved.
  Both failed native roll experiments stay disabled. Live acceptance is pending.

# 0.2.0-coop-test.8

- Let the local native ranged turn complete before firing. Both keyboard and
  controller input trees wait for the character to face their selected target;
  the helper no longer overwrites controller rotation during that task. The
  camera keeps its independent view. Native targets, charging, ammo and ability
  timing are unchanged.
- Require the verified local controller UI-capture flag before a surviving
  menu widget can pause first person. Real menus still pause normally; unknown
  or unreadable state remains an error, with no timeout override.
- Check saved object-array identity before reading retired mob meshes. Restore
  surviving detached components before dropping their records. Live unreadable
  objects remain restoration failures; retired objects are never written.
- The stationary firing stall was observed with physical trigger input, ammo
  and native ability state. The food-associated report's exact cause remains
  unconfirmed. These changes require live confirmation in the new build.
- Preserve smooth camera pacing, guarded camera recovery and both disabled
  native roll experiments. Backward rolling and the game crash remain open.

# 0.2.0-coop-test.7

- Fix the cursor-targeter replacement path identified in the friend's test.6
  log after firing a bow. Restore the old object's own override when it is
  still valid, capture the new local targeter's baseline, and continue first
  person. Serial reuse never receives the old object's saved value.
- Prove cursor retirement through its saved UObject index/serial before
  reading freed memory. Unreadable live slots and real restore failures still
  block activation; ownership, layout and executable guards remain required.
- Keep cursor objects and the optional roll-strength curve out of critical
  camera identities. Curve retirement cancels steering and retains native
  dodge/aim instead of switching off the entire view.
- After a failure, restore first and retry fresh player discovery with delays
  of 0.5, 1 and 2 seconds. Stop after three automatic retries. F6 can retry
  restoration and activation; F5 restores only. Discovery retains selection
  while waiting for a playable player. Logs name restoration blockers.
- Preserve test.6's turning timing changes and native bow timing/targets.
  335 repository tests pass. Live confirmation of test.7 is pending.
- Both failed native roll experiments remain disabled. Backward rolls and
  the access-violation game crash remain unresolved. A helper can recover
  while the game is running; it cannot revive a terminated game process.

# 0.2.0-coop-test.6

- Include work inside the 120 Hz frame budget instead of sleeping a full
  additional frame after every update. Apply camera rotation before binding,
  equipment and scene work; retain native parent propagation of world position.
- Traverse mob mesh attachments and refresh their depth flags incrementally
  with a small per-frame budget. Recheck serials and attachment ownership when
  each slice resumes; restoration cancels unfinished discovery.
- Add bounded per-profile timing logs and record the game's process exit code.
- The user confirmed smoother keyboard/controller turning in single player.
  Long-session and online behavior still need confirmation.
- A separate two-call target-data experiment supplied backward direction before
  native roll commitment. It passed x64 instruction emulation and ownership /
  transaction tests; the user rolled backward, then the game closed. It is now
  blocked before allocation, thread pause or game writes, and its CLI is rejected.
  No matching dump/error for that exit was found. Both native experiments remain
  quarantined. Backward roll is unresolved; this release uses the prior data-only
  roll behavior and does not include an enabled native experiment.
- 321 repository tests pass, including incremental ownership checks, camera
  update ordering, instruction emulation and refusal of both failed experiments.

# 0.2.0-coop-test.5

- Keep the first-person camera attached during villager dialogue, quest
  acceptance and MiniInventory. Return native input and cursor control while
  these widgets are active; closing them reuses the same view and look direction.
- A live capture showed dialogue switching the local manager's view target to
  its villager despite the player camera staying at eye height. Keep known
  quest-villager targets on the existing player camera, with local ownership,
  non-replication and restoration guards. Other view targets pause first person
  and restore the body. The final guard is not yet live-confirmed.
- Read rebuilt Enhanced Input arrays coherently with bounded retries. A
  transient rebuild skips input writes for that frame without disabling the view.
- Refresh recycled controller trigger headers, preserve unfamiliar runtime
  triggers and skip retired input sources. Supported source contexts still
  require the verified layout and all restoration errors remain reported.
- Remove retired controller-array records after complete snapshots, restore
  surviving inactive thresholds before removing them, and log update timing /
  record counts for the reported worsening jitter during longer sessions.
- Address the observed helper suspension while the game stayed open. Native
  roll hooks remain disabled after the earlier failed trial. Backward/left
  roll and distant co-op jitter remain unresolved.
- 304 repository tests pass, including UI handoff, cursor-bit preservation,
  mapping rebuilds, local view targets and refusal of live restoration errors. Live visual menu
  confirmation remains pending.

# 0.2.0-coop-test.4

- Support updated Steam build 25647713 with the original data-only camera and
  controls. Preserve exact executable, reflection, instruction and local-player
  ownership guards. First person activated in the updated live game.
- Quarantine the native roll experiment after the first controller/co-op trial:
  the user reported backward/forward travel reversed, followed by a game exit.
  No matching crash dump was found and the exit cause remains unconfirmed.
- Refuse native installation before allocation, thread pause or game writes;
  reject its CLI/startup option, remove the checkbox and clear saved opt-ins.
  Test these refusal paths while retaining offline transaction/stub tests.
- All 274 repository tests pass. Preserve previous archives, select test.4 as
  the updated download, and record the failed prototype separately.
- Co-op backward roll jitter, ordinary camera jitter, keyboard click/R/S and
  the friend's view remain unconfirmed. No directional roll fix is claimed.

# 0.2.0-native-roll-prototype.4 (failed live trial; quarantined)

- Port the exact executable guard to Steam build 25647713 after the game update.
  Revalidate live reflection, local possession, camera/input/presentation layouts,
  cursor vtable and every original native roll instruction before the live trial.
- Update build-specific RVAs and rel32 operands; retain refusal of other hashes.
  Keep the native experiment off by default and preserve all older archives.
- Make transaction fault tests follow the selected patch sites; verify both
  direction calls resolve to the bool getter and all redirects are covered by
  busy-thread exclusions. All 271 repository tests pass.
- Start a separate source controller/co-op trial with the native roll option
  enabled. Its directional roll and observer outcomes remain pending.
  Earlier keyboard click/R/S reports and ordinary camera jitter remain open.

# 0.2.0-native-roll-prototype.3

- Connect native keyboard R presses to the existing roll-force direction
  helper using the same sampled WASD axes as movement. Preserve native dodge
  creation/cooldown and controller priority; no keyboard event is injected.
- Remember runtime key caches for inherited disabled S/movement bindings.
  Repair rebuilt keyboard and force-attack mouse rows before the periodic
  discovery deadline, with bounded mapping-array snapshots and guarded writes.
- Add six regression tests. Keyboard dodge travel, click movement and the
  S+dodge wheel report still need a focused live result; no co-op/controller
  result is claimed. Native hooks remain off for the current source trial.
- Keep stable test.3 selected and preserve the previous prototype archives.

# 0.2.0-native-roll-prototype.2

- Let F5 retry the helper's own partial patch bytes after both a write and
  rollback fail; retain original page protection through recovery.
- Block reactivation until recovery completes. Reject unrelated bytes and
  changed surrounding guards before restoration or rollback writes.
- Add offline fault tests for incomplete installation/restoration, page
  protection failures and foreign-code rejection. Native stubs are unchanged.
- Keep stable test.3 selected and preserve prototype.1 archives. No live game
  patch or roll test has occurred; backward roll and camera jitter remain open.

# 0.2.0-native-roll-prototype.1

- Prepare the explicitly authorized native roll design for a later test.
  No game patch or live test has occurred; the session ended first.
- Add an experimental launcher/CLI option, default off. Local ownership gates
  select native directional/left-stick paths before predicted target data.
  Keep the native roll action/cooldown/physics and disable late force editing.
- Add fixed instruction guards, bounded thread pause/context checks, RX/RW
  separation, read-back/cache flush, transaction rollback and restoration.
- Check x64 registers, flags and stack stability with offline emulation; test
  Windows API operations against a disposable helper process.
- Retain stable test.3 as the current download. Native roll, host/observer
  behavior and camera jitter remain unverified or unresolved.

# 0.2.0-coop-test.3

- Restore right-stick look after rejected native-input experiments. The player
  confirmed that looking no longer initiates dodges in guest co-op.
- Disable the native right-axis dodge key by name as well as threshold,
  preserving its FKey cache and guarded menu/rebuild restoration.
- Suppress duplicate directional R3 rows while first person is active;
  preserve the confirmed forward R3 action and LB emote swap.
- Quarantine the failed native-dodge-input module and remove its CLI route.
  Preserve the confirmed bow, mouse, cursor, movement and zone-camera fixes.
- Backward R3 jitter remains unresolved. Ordinary camera jitter, host behavior
  and the remote observer's view remain separate test items.

# 0.2.0-coop-test.2

Live guest co-op corrections, tested while the game stayed open.

- Keep native bow candidates, selected targets and timing. The old ranged
  filter could animate a shot without releasing an arrow in guest co-op.
- Move the left stick through native pending movement input alongside WASD,
  so backpedaling no longer redirects the bow's native targeting task.
- Left-click uses native KBM force-attack behavior instead of click-to-move;
  Shift+left-click retains normal interaction and pickups.
- Hide the game's own mouse cursor in first person; restore it for menus
  and the normal camera. Cursor/melee/menu behavior confirmed by the player.
- Include the new input/cursor modules and their guarded restoration checks.

Backward R3 rolls can still be intermittent in guest co-op. Host behavior,
server corrections and the friend's view remain test items.

# 0.2.0-coop-test.1

Co-op test package of the current offline-playtested source. Online
compatibility and remote views await host/guest testing.

- Two-second L3 toggle and the confirmed faster 180-degree quick turn.
- Camera-relative WASD, camera-facing melee and native bow charging/scoring
  filtered to mobs ahead; A-button pickups retained.
- R3 roll direction from the left stick, with an independent comparison switch.
- F4 suppression of full white/red mob silhouettes, including custom-depth meshes.
- Explicit LocalPlayer ownership, possession, identity and network-role checks.
- Comparison settings for native movement/combat/mesh facing.
- Optional read-only co-op snapshots, Ctrl+F11 and host/guest observer guide.
- New Windows/source archives, checksums and offline smoke tests.

No terrain/game assets, authentication changes, DLL loader or network calls
are included. Movement/aiming/dodge adapters change game actions and are not
wholly cosmetic. Online roll prediction remains a specific test item.

# 0.1.0-beta.1

First shareable Windows release, based on the locally playtested prototype.

- First-person camera with mouse/right-stick look and eased F6 switching.
- Direct player attachment prevents native zone zoom from pulling the view.
- Arms and weapons retained, with local body/armor hiding and restoration.
- R3 forward dodge and LB emotes, restored to native bindings on exit.
- Automatic resume after inventory, map, pause and emote menus.
- First-person view retained across Alt-Tab, with look input paused.
- Undirected damage vignette rendered in a separate process.
- F4 toggle for through-wall mob silhouettes.
- Desktop launcher, saved startup settings and graceful Stop & restore.
- Automatic discovery of the running game's installation, exact-build check.

Terrain gaps/pop-in, remaining mob feet, directional damage and multiplayer
validation remain outside this release. The packaged runtime passes offline
checks separately from the prototype's in-game visual confirmations.
