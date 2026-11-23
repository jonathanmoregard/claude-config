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

One run does three steps, all against `$HOME`:

1. `import_claude.py` reads `~/.claude` and `~/.claude.json` (read-only) into
   a manifest at `codex/imports/claude-live/manifest.json` (gitignored).
2. `render_codex.py` builds the render plan from that manifest and publishes
   it into the managed paths: `~/.codex/{config.toml,rules,scripts,hooks,
   hooks.json,mcps,agents,skills,AGENTS.md,claude-setup-mirror}`, the
   `~/.codex/plugins/claude-mirror` link, `~/.agents/plugins` and
   `~/plugins/claude-mirror` (`managed_paths()` is the list).
3. `check.py` compares disk with the renderer's own plan and holds the output
   to safety floors it states independently (permission profile, Git broker,
   GitHub tool exclusions, no persisted exec-policy allow rule, no hook the
   blanket disable guard turns off, ...). The sync exits non-zero if it fails.

Production renders into a staged home, checks it, and publishes only what
passed. A direct run renders in place (the output names absolute paths under
`$HOME`), then checks; when the check fails it puts every managed path back
as it was before the run, `config.toml` included, and exits 1. The importer's
manifest is still updated.

`~/.codex/<name>.config.toml` files, which `codex --profile <name>` layers over
`config.toml`, are outside the mirror: the sync neither writes nor removes
them. The check layers each over `config.toml` the way Codex does and fails,
naming the file, when the result breaks one of `config.toml`'s safety floors
or points an agent role at a file that does. Keys that cannot widen a session,
such as `model`, pass.

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
