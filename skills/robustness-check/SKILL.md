---
name: robustness-check
description: When running e2e tests repeatedly (20+ repetitions) for robustness or flakiness checking, pause the autodoro service to prevent interruptions during the long test run.
user-invocable: false
---

## When this applies

When you are about to run the full e2e test suite 20 or more times in a row (robustness checks, flakiness detection, stress testing), pause autodoro before starting.

## Before testing

```bash
systemctl --user stop autodoro-restart.timer 2>/dev/null; true
systemctl --user stop autodoro.service
systemd-run --user --on-active=1h --unit=autodoro-restart systemctl --user start autodoro.service
```

## After all repetitions complete

Restart autodoro immediately rather than waiting for the timer:

```bash
systemctl --user stop autodoro-restart.timer 2>/dev/null; true
systemctl --user start autodoro.service
```
