SessionStart hook output:
[unit-failure] security-batch-dependency-health.service FAILED at 2026-10-24T03:17:42Z (result=exit-code status=1): security-batch dependency-health: exit 1: 1 repo needs attention; widget: 1 alert 1 high; accept in GitHub: dismiss alert or @dependabot ignore — inspect: ls {{E2E_RUN}}/var/lib/security-batch/dependency-health/latest/; journalctl -u security-batch-dependency-health.service -n 200

morning. the nightly check is red, take care of it. widget is checked out here.
