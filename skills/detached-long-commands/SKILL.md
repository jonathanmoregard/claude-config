---
name: detached-long-commands
description: Use when a command will run longer than the 10-minute safe-bash cap or must survive the session — nix builds, VM test gates, large downloads/ingests, feature-vm launches, index builds. Triggers on safe-bash exit 124 / "command exceeded timeout", or any build you'd otherwise babysit.
---

# Detached Long Commands

safe-bash hard-caps at 600000 ms, and `nohup`/`&` children die with the
session. The proven pattern (~20 uses across VM gates, feature-vm boots,
and register-index ingests):

## Launch — detached systemd user unit + sentinel markers

```bash
systemctl --user reset-failed <slug>.service 2>/dev/null  # if reusing the name
systemd-run --user --unit=<slug> --working-directory=<dir> bash -c \
  '<cmd> > /tmp/<slug>.log 2>&1 && echo MARKER_OK >> /tmp/<slug>.log \
   || echo MARKER_FAILED >> /tmp/<slug>.log'
```

- **Always append sentinel markers** (`GATE_ALL_GREEN`/`GATE_FAILED`
  style). Polling greps for them — never infer success from silence or
  an empty log.
- The unit name is the rerun handle; `reset-failed` before reuse.

## Poll — bounded loops, tail for visibility, never kill

```bash
for i in $(seq 1 58); do
  grep -qE 'MARKER_OK|MARKER_FAILED' /tmp/<slug>.log && break; sleep 10
done; tail -3 /tmp/<slug>.log
```

- ~10 min per poll call (fits the cap); repeat calls as needed. Tail the
  log each round so progress is visible between polls.
- **Do NOT kill a slow build to retry** — killing wastes every finished
  derivation. Nix builds with big artifacts look wedged while the cachix
  post-build-hook pushes (each push has its own 600 s cap and re-fires
  per referencing derivation); that's slow, not stuck.

## Readiness variant (services/VMs instead of builds)

Poll a real probe, not elapsed time: SSH banner
(`ssh-keyscan -p <port> -T 5 127.0.0.1 | head -1`), a TCP connect, or a
file's existence — each wrapped in `timeout N …` so one hung probe can't
eat the loop. A launcher log's last line ("building …drv") tells you
which phase you're waiting on.

## Red flags

- `nohup`/`&` for anything that must outlive the session
- Asking to raise the safe-bash timeout instead of detaching
- Concluding failure from an empty/quiet log without the FAILED marker
- Killing an in-progress nix build out of impatience
- `sleep 600` with no grep — poll loops must be able to exit early
