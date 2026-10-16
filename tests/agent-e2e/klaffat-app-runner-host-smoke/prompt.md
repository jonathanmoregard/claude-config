Use the Klaffat app runner tool (`klaffat_app`) to start the Klaffat app in
`app` mode from the worktree `/home/jonathan/worktrees/klaffat-app-runner-host-smoke`.
Wait until it is ready, fetch the home page `/` through the tool, then stop the
run. Use only that tool for this, not Bash or any other HTTP client.

Your final answer is one line:

- `APP_RUNNER_PASS <run_id> <text of the home page's <title> element>` if all of that worked;
- `APP_RUNNER_UNAVAILABLE` if you have no such tool;
- `APP_RUNNER_FAIL <reason>` otherwise.
