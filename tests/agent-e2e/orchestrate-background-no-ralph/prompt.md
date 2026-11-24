/orchestrate I'm away for the next hour, so run this unattended and follow the full orchestrate procedure, every step, not a shortcut. In this repo:

1. Add a `wordcount(text: str) -> int` function to `textutil.py` (whitespace-separated words; empty string is 0) with pytest tests in `test_textutil.py`.
2. Add a `title_case(text: str) -> str` function to `textutil.py` with pytest tests.
3. Have a background subagent run the slow integration check `./integration.sh` and report the `RESULT:` line it prints. The subagent should run it directly with Bash (timeout 600000 ms) and wait for it; it takes about five minutes. Do not run it in the main session.

Commit locally when done; no push and no PR. When everything is finished, tell me the integration RESULT value.
