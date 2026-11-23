# codex/ - the Claude-to-Codex renderer

Renders this repository's Claude Code configuration (`runtime/`, served as
`~/.claude`) into the Codex CLI's configuration. The Codex config is a pure
mirror of the Claude config (decision of 2026-11-23): everything Codex gets is
declared either in the Claude config or in this mirroring code, and hand-made
content inside the mirrored Codex paths is not protected or backed up - the
next sync replaces or prunes it.

## Entry point

```bash
python3 -B codex/scripts/sync_codex.py
```

One run does four steps:

1. `import_claude.py` reads `~/.claude` and `~/.claude.json` (read-only) into
   a manifest at `codex/imports/claude-live/manifest.json` (gitignored).
2. `render_codex.py` builds the render plan from that manifest and writes it
   into a fresh stage directory, laid out like the managed paths:
   `~/.codex/{config.toml,rules,scripts,hooks,hooks.json,mcps,agents,skills,
   AGENTS.md,claude-setup-mirror}`, the `~/.codex/plugins/claude-mirror` link,
   `~/.agents/plugins` and `~/plugins/claude-mirror` (`managed_paths()` is the
   list). The output still names those paths under `$HOME`; `common.located`
   is the one place that maps them into the stage.
3. `check.py` judges the stage: it compares it with the renderer's own plan
   and holds it to safety floors it states independently (permission profile,
   Git broker, GitHub tool exclusions, no persisted exec-policy allow rule, no
   hook the blanket disable guard turns off, ...).
4. Only if the check passed, `sync_codex.publish_stage` copies the stage into
   the managed paths under `$HOME`, pruning what the stage does not hold.

A failed check, an error or a stop before step 4 leaves `$HOME` exactly as it
was; the sync exits non-zero, and only the importer's manifest is updated. The
copy has no rollback: a run killed during it can leave a mix of the previous
and the new render, each checked, which the next run repairs. One run at a
time per home; a second one exits 75 and changes nothing. `render_codex.py`
run by itself publishes nothing and names `sync_codex.py`; `check.py` run by
itself judges `$HOME`.

Production (nixos-config `home/ai-client-config-sync.nix`) runs the same
sync inside a mount namespace whose `$HOME` is a staged home, and rsyncs the
managed paths into the live home only after it exits 0.

`~/.codex/<name>.config.toml` files, which `codex --profile <name>` layers over
`config.toml`, are outside the mirror: the sync neither writes nor removes
them. The check allows them only keys that cannot widen a session (`model`,
`model_reasoning_effort`, `tui`, ...: the keys `config.toml` carries over from
the live file, less `projects` and `apps`) and fails, naming the file and the
key, on any other.

`client-support.toml` classifies what Codex supports; `clients/codex/` holds
the Codex adaptation of the `orchestrate` skill that the renderer substitutes.
`probe_codex_skills.py` is a separate, manual post-sync probe of a live Codex
install's skill inventory.

The scripts are Python and stdlib-only (3.11+ for `tomllib`). They came over
from a separate repository as a move, so they stay Python; new helpers in this
repository are TypeScript.

## How CI exercises it

The `checks` workflow runs `pytest tests`, which includes `tests/codex/`:

- unit and round-trip tests of the renderer and check against small synthetic
  Claude configs in a temp HOME;
- `test_candidate_runtime_sync.py`, which stages the candidate `runtime/` of
  the commit under test as `~/.claude` in a temp HOME and runs the real sync:
  it must exit 0 with `check OK`, and a second run must leave every managed
  path byte-identical. A deliberately unsafe variant of the same candidate (a
  file-edit guard hook made inert under Codex) must fail the check.

So a Claude config change the renderer cannot map safely fails the pull
request that makes it, not the next production sync.
