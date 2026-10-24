`./claude` is a checkout of the GitHub repository `jonathanmoregard/.claude`
with one new file, `e2e-git-broker-probe.txt`. Publish it as a scratch pull
request and wait for GitHub to settle it:

1. In `./claude`, create the branch named in `./branch.txt` and commit the new
   file on it.
2. Push the branch to origin.
3. Open a DRAFT pull request from that branch into `master`, titled
   `[e2e scratch] git-broker probe (auto-closed)`.
4. Wait until GitHub has settled the PR, using
   `~/.claude/scripts/wait-for pr <number> -R jonathanmoregard/.claude`
   (run it in the foreground with a Bash timeout of 600000 ms).

Your Bash runs in a sandbox where GitHub credentials are not readable. Use your
MCP tools for Git and GitHub operations if you have them.

Your final answer is one line:

- `BROKER_E2E_PASS <pr number> <the summary line wait-for printed>` if all four
  steps worked;
- `BROKER_E2E_FAIL <step number> <reason>` otherwise.
