# Codex RSI Flow Design

## Goal

Make top-level Codex sessions produce bounded, cache-efficient session
reflections that feed the existing Recursive Self-Improvement proposal review,
without exposing source transcripts to another provider or allowing reflected
content to invoke tools.

## Current failure

The Claude-to-Codex mirror correctly registers and fires `SessionEnd`, but it
copies a Claude-specific command. `session-reflect` invokes
`claude --resume <session-id>`, which cannot resume a Codex session, and its
fallback parser understands Claude JSONL rather than Codex `response_item`
records. Even a successful reflection is not consumed by RSI: the nightly
reviewer reads raw files under `~/.claude/projects/` and is currently disabled.

## Chosen architecture

Keep one normalized reflection format and make model invocation client-aware.

For Claude transcripts, preserve current behavior: fork-resume through the
Claude CLI, retaining its prompt-cache benefit and no-tools boundary.

For top-level Codex `SessionEnd`:

1. Validate that `transcript_path` resolves under `~/.codex/sessions/`.
2. Create a mode-0700 scratch `CODEX_HOME` under the user runtime directory.
3. Copy only the source rollout and Codex authentication file into that scratch
   home. Never point the reflection process at the live rollout: a live probe
   proved `codex exec resume --ephemeral` appends to the resumed transcript.
4. Resume the copied session with the same model, low reasoning effort, hooks,
   shell, apps, browser, computer use, and image generation disabled. Use
   `--ignore-user-config`, read-only sandboxing, and a strict output schema.
5. Parse the JSONL event stream into the existing synthetic result envelope,
   then reuse the existing validation, artifact, manifest, and usage paths.
6. Remove only the exact scratch directory created by this invocation.

Resuming an identical copied rollout keeps API prompt-cache eligibility while
leaving the source byte-for-byte unchanged. The same-provider path also avoids
shipping Codex session content to Anthropic.

Codex `PreCompact`, `mission`, and `SubagentStop` reflection calls will skip
without a model invocation. They cannot use a safe non-mutating resume today,
and Codex subagents already appear in the parent session outcome. This avoids a
call multiplier: recent evidence showed 37 subagent reflection attempts versus
12 top-level session-end attempts.

## RSI input and resource bounds

Nightly RSI will read normalized top-level `sessionend` `final.json` artifacts,
not raw chat logs. It will:

- consider only files modified during the last 24 hours;
- exclude nested `subagents/` artifacts;
- inspect at most 12 newest completed reflections;
- never follow `source_transcript` paths;
- keep the existing two-proposal output limit and content-dedup sink.

Reflection normalization will enforce five arrays, at most ten strings per
array, and at most 500 characters per string. Codex model output is additionally
constrained by JSON Schema. These bounds prevent one runaway reflection or one
busy day from turning the nightly pass back into a transcript-sized context
dump.

After focused and full offline tests pass, set `components.daily_review` to the
boolean `true`. Authentication or provider-quota failures remain terminal,
reasoned manifests; no fallback crosses provider boundaries.

## Security properties

- Transcript path must be a regular readable file beneath an allowed client
  session root.
- Reflection subprocess gets no agent tools or mirrored hooks.
- Codex authentication copy stays mode 0600 inside mode-0700 runtime scratch.
- Raw transcript content never enters proposal files.
- Source session stays immutable; test compares digest before and after.
- Model output is schema-checked, type-coerced, count-capped, and length-capped
  before persistence.
- Existing proposal sink retains filename validation, size cap, atomic publish,
  secret scan, and dedup ledger.

## Acceptance checks

1. Codex fixture reaches one native resume call and never calls Claude.
2. Source rollout digest stays unchanged.
3. Reflection subprocess arguments disable hooks and tool-bearing features.
4. Valid Codex output produces `status=complete`, `source_client=codex`, and a
   bounded `final.json`.
5. Invalid output and subprocess failures produce terminal error manifests.
6. Codex non-session-end modes make zero model calls.
7. Claude fixtures and existing 120 focused assertions remain green.
8. RSI prompt reads only bounded completed reflections and config enables the
   reviewer.
