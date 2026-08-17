# Contamination adjudication — one pattern narrowed, on the record

Date: 2026-08-17. Written before any run, after the first `contamination` pass
returned DIRTY. Recorded separately so the change is auditable rather than silent.

## The failure

Pattern `session is (still )?alive` (must_be_absent) returned 1 hit at the pin:

```
7958ede:skills/session-reflect/reflect.sh:778
  # Synchronous: precompact runs while session is still alive.
```

Full context (lines 777–782 of the pinned file):

```bash
if [[ "$MODE" == "precompact" ]]; then
  # Synchronous: precompact runs while session is still alive.
  # Holds fd 9 the whole time — natural lock retention.
  run_worker
  exit 0
fi
```

## Ruling: not contamination. Pattern was mis-aimed.

The pattern was intended to catch the *conclusion* recorded later in `4129f79` —
that because self-compact fires while the session is alive and already holds the
context, the session should write its own handoff instead of spawning a resumed
worker.

What it actually caught is an *observation* about lock retention in an unrelated
code path. And the pinned tree states the opposite of the conclusion: even in
precompact mode — with the session demonstrably alive — reflect still invokes
`claude -p --resume <sid> --fork-session` (pinned `reflect.sh:47`). The tree
therefore contains the premise and does **not** contain the inference.

That distinction is the whole point of the experiment. The alive/ended clue was
available in-tree at the time and was not acted on; that is the failure being
replayed. Deleting the clue would make the task easier than the original and
destroy its status as a replay. Leaving a pattern that fires on the clue would
block the experiment for the wrong reason.

## Change made

In `config.json`, `must_be_absent`:

- removed: `session is (still )?alive`
- added:   `(alive|live) session.{0,100}(writes?|producing) (its|the) own`
- added:   `wrote the handoff itself`

The replacements target the recorded conclusion, not the premise. All other
patterns unchanged. No other config key touched.

`PREREGISTRATION.md` is deliberately **not** edited — the primary outcome,
non-qualifiers, secondaries, analysis plan and decision rule are exactly as
locked. Only the contamination gate's aim changed, and only in the direction of
precision.

## Disclosure

I proposed the change under test, wrote both arms, the task prompt, the rubric,
the original contamination patterns, and this adjudication. There is no
independent reviewer of this ruling. A reader who disagrees should treat the
pinned `reflect.sh:778` comment as a partial in-tree hint available to both arms
equally — it cannot favour one arm over the other, since both arms read the same
tree.
