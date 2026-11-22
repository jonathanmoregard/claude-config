Orchestrate this, unattended, in this repo:

1. Add a `wordcount(text: str) -> int` function to `textutil.py` (whitespace-separated words; empty string is 0) with pytest tests in `test_textutil.py`.
2. Have a background subagent run the slow integration check `./integration.sh` (it takes about a minute and a half) and report the `RESULT:` line it prints. Do not run it in the main session.

Commit locally when done; no push and no PR. When both are finished, tell me the integration RESULT value.
