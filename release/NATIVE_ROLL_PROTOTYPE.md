# Native roll prototype — failed live trial, installation disabled

Test.6 separate target-data trial (2026-10-02): two calls to the pawn-forward
getter inside GA_Roll were redirected to a local serial/possession/root-guarded
leaf function. It supplied a coherent camera-relative vector before normal
target-data commitment, without changing action mappings or editing forces.
The code passed x64 instruction emulation, nonvolatile/stack checks, foreign
fallback and partial-write rollback tests. The user reported smoother turning
and successful backward travel, then the game went straight to the desktop.
Steam recorded exit code 0 for that exact PID. No matching dump or application
error was found. The capture window had ended before this live activation, so
there is no measured post-patch travel or faulting instruction.

This second experiment is also unavailable: ROLL_TARGET_DATA_AVAILABLE=False,
prepare refuses before allocation/thread pause/writes, and --roll-target-data
is rejected before opening the game. The source helper was gracefully stopped.
Both code-modifying experiments remain quarantined; neither should be revived
from its emulator result alone. Test.6 retains the confirmed camera timing
improvements and the previous data-only controls. Backward roll is unresolved.

On 2026-10-02, the first controller/co-op trial on Steam build 25647713
activated first person and installed the three guarded native redirects.
The user reported backward input rolling forward and forward input rolling
backward, followed by the game closing. Steam recorded exit code 0; no matching
crash dump or Windows application error was found. The exact exit cause is
unconfirmed. The helper verified local ownership and a separate remote pawn;
only the local mesh's 16 body bones were hidden in the captured snapshot.

The experiment is **unavailable in 0.2.0-coop-test.4 and test.5**. CLI/startup settings
reject it, saved opt-ins are cleared on load, and its checkbox is removed.
NativeRollHook.prepare refuses it before allocation, thread pause or writes.
The failed helper was gracefully stopped after the game exited and replaced
with a source helper with native hooks disabled. Game restart retires the
failed process's transient patches.

The supported download uses the existing data-only camera and controls,
including the original roll-force helper. Backward co-op jitter, ordinary
camera jitter, keyboard click/R/S and observer validation remain open. The
historical prototype.4 archive is retained as failed evidence, not for play.

## Historical design and evidence

The approved prototype redirected three five-byte instructions to leaf stubs
that selected direction before native predicted target data was queued:

| Steam build 25647713 RVA | Original instruction | Intended local behavior |
| --- | --- | --- |
| `5AAD599` | Call IsDirectional getter | Return true for matching local activation |
| `5AAD752` | Call the same getter | Return true for matching local activation |
| `5A997FB` | Set analog-stick enum 1 | Select enum 0, the native left stick |

Native abilities, target data, collision, cooldown and force creation remained
active. Late force editing was disabled during the trial. Ownership gates
checked local controller/pawn pointers, indices, serials, bidirectional
possession, LocalPlayer identity and role. Other calls retained the original
getter or stick enum. There was no DLL, remote thread, UE4SS installation,
extra native call, analog-memory mirror or custom RPC.

Offline emulation verified nonvolatile/XMM registers, arithmetic flags, stack
stability and foreign-pointer rejection. Transaction tests covered readback,
rollback, restoration, partial writes, original page protection and unrelated
code rejection. Windows API smoke used a disposable Python process. Read-only
live inspection verified exact hash, reflection, instruction bytes and local
ownership before the trial. These checks did not establish live safety or
correct co-op travel; the user's result takes precedence.

Current offline transaction tests use fake memory. Refusal tests cover
allocation/pause/writes, CLI entry and saved startup preferences. The game
exit cause and direction inversion need investigation before a replacement
native trial. All 274 repository tests pass with installation disabled.
