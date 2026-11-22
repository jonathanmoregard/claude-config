#!/usr/bin/env bash
# PASS: every new non-test file under runtime/, and every new script elsewhere,
# is a .mts under runtime/ that tsc reaches, and no package.json sets
# "type": "module"; test files are fine.
# FAIL: a new .ts, any other new helper (any other file under runtime/, or a
# script outside it), a .mts tsc skips, or a "type": "module" package.json.
exec python3 -I "$(dirname "$0")/../_lib/helper_lang.py" new-ts "${1:?run dir}"
