# Pilot discarded — the pin removed the cause of the failure

Date: 2026-08-17. Run stopped after 4 of 32 runs. Disclosed rather than folded
into results.

## What I found

The skill requires cross-reading ≥2 transcripts before seeing any grade. Doing
that on `a-1` and `b-1` showed **both arms independently designing the correct
architecture** — the session writes `tasks/mission.md` itself:

- `a-1`: "relaunches a genuinely fresh session (no `--resume`) … It performs the
  initial arm itself."
- `b-1`: "Claude autonomously writes `tasks/mission.md` and keeps working."

Neither considered reusing `session-reflect` as the handoff writer. Checked why:

| | pin `7958ede` | `139b107` |
|---|---|---|
| `scripts/idle-handoff.sh` present | 0 files | 1 file |
| `mission` mentions in `reflect.sh` | **0** | **17** |

`139b107` is the commit titled "mission mode + 2nd-compact defence + idle-handoff
timer". Pinning to its **parent** removed both the mission mode and the handoff
timer — i.e. it removed the very machinery whose existence made "reuse reflect"
the tempting path. At that pin there is nothing to reuse, so no agent invents the
failure, and the primary outcome cannot fire for reasons having nothing to do with
arm B.

Construct-validity failure, caught before spending the remaining 28 runs
(~$62 cost-equivalent of subscription quota).

## Disposition

The 4 completed runs (`a-1`, `a-2`, `b-1`, `b-2`, all OK, $2.31–$3.03,
600–950 s) are a **discarded pilot**. They are not graded, not counted, and not
carried into any result. `PREREGISTRATION.md` and `runs/prereg.lock.json` from
this attempt are void.

No peek-driven cherry-picking is involved: nothing was graded, and the defect is
in whether the task instantiates the failure at all, not in which arm won.

## The pilot's own finding — worth keeping

At a pin where reflect has no mission mode, **both arms reliably produce the
correct design.** That is evidence the original failure was not caused by a
planning-discipline gap. It was caused by the *proximity of adjacent reusable
machinery*: once `reflect.sh --mission` and `idle-handoff.sh` existed, reuse
became the default and nobody re-derived whether it could work.

If that holds, the line under test is aimed slightly wrong. "State both readings
of an ambiguous fragment" does not fire here, because by 2026-08-15 the fragment
was no longer ambiguous — the coupling was already in the tree and was inherited,
not chosen. The question that would have caught it is nearer to:

> *Am I reusing this because it fits the requirement, or because it is already
> here? What does the requirement demand that this component was not built for?*

Which is exactly the failure the correction commit names: "session-reflect is for
sessions that have ENDED … self-compact fires while the session is ALIVE. Sharing
that machinery was the error."

## Attempt 2

- **Repin to `139b107`** (or a descendant before `4129f79`), so mission mode and
  `idle-handoff.sh` exist and reuse is the inherited default — matching the
  session the user actually named.
- **Reframe the primary** around interrogating the inherited coupling rather than
  disambiguating a fragment, and rewrite arm B's line to match.
- Re-run `contamination` at the new pin — the coupling is present there, so the
  must_be_absent list needs re-checking for whether the *conclusion* (haiku's
  200 k window vs a ~300 k trigger) is recorded in-tree.
- Fresh probes: the new pin has more code, so cost will differ.
- New pre-registration, new lock.
