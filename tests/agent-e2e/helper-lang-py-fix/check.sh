#!/usr/bin/env bash
# PASS: list_skills.py itself is fixed, by its printing line alone; a new test
# file is fine.
# FAIL: the .py was removed, or any other new script (a port) was written.
exec python3 -I "$(dirname "$0")/../_lib/helper_lang.py" py-fix "${1:?run dir}"
