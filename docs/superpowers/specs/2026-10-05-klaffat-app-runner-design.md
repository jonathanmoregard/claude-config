# Klaffat app-runner MCP design

Date: 2026-10-05

## Outcome

Claude Code and Codex can start a Klaffat development app, run mock-backed
Playwright E2E tests, run the repository's development checks, export the
Claude Design bundle, and start the existing real-calendar test service from
a Klaffat linked worktree. The tools work from an agent sandbox because the
MCP server itself runs on the host.

The capability is intentionally Klaffat-specific. It is not a general command
runner.

## Existing paths retained

The runner composes two existing, reviewed launchers rather than recreating
their responsibilities:

- `scripts/e2e-run` owns the disposable PostgreSQL database, isolated ports,
  mock Google and Microsoft OAuth endpoints, application build, cleanup, and
  durable test artifacts for normal app and E2E modes.
- `/run/current-system/sw/bin/klaffat-local-google` owns the real-provider
  service. Despite its historical name, the installed NixOS module supports
  real Google and optional real Microsoft OAuth. Root decrypts the selected
  worktree's agenix bundle, passes only configured provider pairs to a
  dedicated service account, and never reveals plaintext credentials to the
  invoking user or agent.

Microsoft is available when the selected worktree's public configuration has
a non-placeholder Microsoft client ID and its encrypted bundle has the
matching secret. A worktree without those inputs still supports real Google.
The agent does not inspect the encrypted bundle. Interactive provider login
and consent remain a human browser step.

## MCP surface

One structured tool, `klaffat_app`, exposes these operations:

1. `start` accepts an absolute `worktree_path` and a fixed `mode`:
   - `app`: mock-backed Klaffat kept alive for browser or agent smoke tests;
   - `e2e`: the complete Playwright suite or one validated test spec;
   - `check`: one affected, focused, full, or crate-filtered Rust check;
   - `claude_design_export`: the repository's fixed design-export workflow;
   - `real_calendar`: the installed real Google/optional Microsoft service.
2. `status` accepts a runner-issued `run_id` and reports lifecycle state, URL,
   exit status when complete, and artifact location where applicable. An
   optional `wait_s` (0-600) blocks until the run settles (app ready, or a
   one-shot run finished), so agents need no sleep loop around a cold build.
3. `logs` accepts a `run_id` plus bounded `tail_lines` and returns redacted
   stdout/stderr. It never returns environments, service definitions, or
   decrypted files.
4. `http_get` accepts a `run_id` and an origin-relative `path` and performs
   one GET against that run's recorded loopback origin, without proxies or
   redirects, returning status, content type, `Location`, and a bounded,
   redacted text body. Sandboxed agents have no loopback network; this is how
   they see the app. The caller never supplies a host or scheme.
5. `stop` accepts a `run_id` and stops only the exact registered run. For a
   real-calendar run it delegates to the fixed host launcher; for other runs
   it stops the runner-owned user service.

`e2e` may additionally receive `spec`, a repository-relative path under
`tests/e2e/tests/` ending in `.spec.ts`. No Playwright flags, grep expressions,
shell text, environment assignments, or arbitrary arguments are accepted.

`check` requires one `check_kind`. `affected` always runs against
`origin/main`; `full` has no extra input; `focused` requires one
`focused_target`: either a canonical existing `tests/e2e/tests/*.spec.ts`
file, a package name read from `crates/*/Cargo.toml`, or a `focused_tests` ID
from `scripts/test-lanes.json`. `rust_test` accepts one Cargo package name and
optional test-name filter. The surface cannot express flags, traversal, shell
metacharacters, or another command.

Every start returns a random opaque run ID. The ID maps to runner-owned state
under the user's private state directory. App and one-shot modes run as
transient user-systemd services so they survive individual MCP calls and their
logs remain inspectable. The real-calendar mode remains a system-service
singleton because the existing trusted launcher owns port 3740 and its
persistent provider-test database.

Units get a per-worktree `HOME` at `target/klaffat-app-runner/home` (inside
the worktree's ignored build directory, created by the unit itself), so
Cargo and npm caches survive between runs and E2E failure artifacts stay
readable from the worktree after the unit and its runtime directory are gone.
`e2e`, `claude_design_export`, and selector-based `check` runs execute
`npm ci --no-audit --no-fund --prefer-offline` in `tests/e2e` first: a fresh
linked worktree has no `node_modules`, and an affected/full/focused selector
may reach Playwright. The committed lockfile decides what Playwright runs.
The unit does not expose the real HOME; system Nix configuration remains
available and Cargo/npm state lives under the per-worktree HOME.

## Validation and security boundary

The server validates before launching anything:

- the worktree path is canonical, absolute, and an immediate
  `/home/jonathan/worktrees/klaffat-*` child;
- it is a Git linked worktree whose common directory is the Klaffat primary
  repository and whose origin matches the configured Klaffat origin;
- required scripts and selected E2E specs are regular files below that
  canonical worktree, not symlinks;
- mode-specific fields are rejected for every other mode;
- run IDs have a strict generated format and state files are private regular
  files owned by the current user;
- unit names, ports, executable paths, environment, output paths, and raw argv
  are derived by policy and never supplied by the caller.

Subprocesses use argv arrays with `shell=False`. Policy configuration is
fail-closed: missing fields, unknown fields, malformed paths, unavailable
fixed executables, or unsafe state permissions reject the operation. Output
is size-bounded and redacts credential-shaped assignments, authorization
headers, OAuth codes, tokens, secrets, and capability-bearing URLs.

Units share the host network namespace. A per-user service manager cannot
attach the eBPF programs behind `IPAddressAllow=`/`IPAddressDeny=`; measured
on this host, a user unit with `IPAddressDeny=any` still connected to a
loopback port, so such properties would only claim a boundary that does not
exist. The builds also fetch crates and npm packages. The trade-off is
explicit: code from the selected worktree runs with the user's network access
(but not the user's home, devices, buses, or kernel tunables), so this runner
must only be pointed at worktrees whose code the operator would run anyway.
Address-family restriction is seccomp-based and is enforced.

The MCP process never opens `deploy/secrets/klaffat-env.age`, runtime provider
environment files, `/proc/*/environ`, or the real-calendar service's private
state. Real-provider secrets remain inside the existing root-to-service
boundary.

## Mode execution

### `app`

The service runs `scripts/e2e-run --command -- <fixed hold helper>`. The helper
prints no environment and waits for termination. `e2e-run` prints the leased
loopback app URL, and its existing cleanup guard tears down the app, mock OAuth
server, and disposable database when stopped.

### `e2e`

The service runs `scripts/e2e-run` with either no argument or the single
validated `tests/e2e/tests/*.spec.ts` path. This keeps Google and Microsoft
mocked and produces the repository's normal durable E2E artifact directory.

### `check`

The service executes exactly one fixed repository entry point after dependency
setup:

- `scripts/check affected --base origin/main`;
- `scripts/check focused <validated-target>`;
- `scripts/check full`; or
- `scripts/rust-test-run -- cargo test -p <validated-crate> [<validated-filter>]`.

`scripts/check` validates its lane manifest and wraps heavy Cargo commands with
the per-worktree `scripts/with-cargo-lock` followed by
`scripts/heavy-test-lease`. `scripts/rust-test-run` independently requires and
uses the same Cargo lock/host lease order while owning its PostgreSQL fixture.
The broker composes those reviewed mechanisms rather than duplicating locks.
Status uses the same running/succeeded/failed phases and exit code reporting as
E2E; logs remain bounded and redacted.

### `claude_design_export`

The service invokes the repository's existing fixed export entry point. The
runner reports its completed artifact location; it does not upload anything
or receive a destination credential.

### `real_calendar`

The runner calls `klaffat-local-google start <validated-worktree>`. That
launcher rebuilds the selected revision, verifies the artifact and worktree,
starts the hardened service, and returns only after `http://localhost:3740`
is healthy. Status and stop use the launcher's fixed subcommands. Starting a
new real-calendar run supersedes any previous activation, matching the host
service's existing singleton semantics.

## Failure and cancellation

Start returns promptly after the transient unit is accepted for app and
one-shot modes; callers poll status and logs. Real-calendar start can take
several minutes to build and is executed in a cancellable MCP worker. MCP
cancellation terminates only the direct launcher process; the host launcher's
own locking and fail-closed activation logic preserve service consistency.

A failed unit remains queryable until normal state retention removes it. Stop
is idempotent for an already-finished owned run. Unknown or tampered run state
is refused rather than guessed.

## Verification

Tests use fake fixed executables and temporary worktree/state fixtures. They
cover schema strictness, path traversal and symlink rejection, repository
identity, spec allowlisting, argv construction, state ownership, lifecycle,
singleton real-calendar behavior, bounded/redacted logs, cancellation, and
malformed policy. No unit test touches real credentials.

An agent-config E2E compares `origin/master` with the staged candidate config:
the control must lack the tool, while the candidate must discover it and
successfully exercise a harmless status/error path. A host smoke then runs a
mock-backed app/E2E action. Real-calendar validation stops at launcher
start/status unless a human completes provider consent; plaintext credentials
must never appear in evidence.

## Rejected alternatives

- An in-process child supervisor loses observability and ownership when the
  MCP process exits.
- A localhost HTTP daemon adds a listening authentication surface without
  improving the stdio MCP workflow.
- A general shell or arbitrary-command tool would turn a narrow testing seam
  into sandbox escape.
- Reimplementing provider decryption in the broker would duplicate and weaken
  the existing NixOS security boundary.
