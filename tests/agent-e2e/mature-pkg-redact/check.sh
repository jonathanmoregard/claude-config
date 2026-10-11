#!/usr/bin/env bash
# PASS: redact() uses a multi-pattern matching package (Aho-Corasick et al.)
# or a stdlib primitive (one compiled re alternation), or hand-rolls only after
# looking a package up and naming it. FAIL: a hand-rolled trie/automaton with
# no package lookup.
exec python3 "$(dirname "$0")/../_lib/grade_handroll.py" "$1" logscrub \
  --pkgs ahocorasick,ahocorasick_rs,pyahocorasick,ahocorasick-rs,flashtext,acora,esmre,hyperscan \
  --handroll '(?i)class\s+\w*(trie|node|automaton|state)\b|\.children\b|children\s*[=:]|\bfail\w*\s*(\[|=)|\bgoto\b|setdefault\(\s*\w+\s*,\s*\{\s*\}\s*\)|\bnode\s*=\s*node\s*[\[.]' \
  --stdlib 're\.compile\('
