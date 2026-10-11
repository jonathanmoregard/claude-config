#!/usr/bin/env bash
# PASS: query.py parses with a parser package (lark, pyparsing, parsy, ...), or
# hand-rolls only after looking a package up and naming it. FAIL: a hand-rolled
# tokenizer + recursive-descent parser with no package lookup.
exec python3 "$(dirname "$0")/../_lib/grade_handroll.py" "$1" issuefilter \
  --pkgs lark,pyparsing,parsy,parsimonious,ply,sly,arpeggio,textx,tatsu,pyleri,funcparserlib \
  --handroll '(?i)def\s+_?(tokeni[sz]e|lex|parse_(or|and|not|expr|term|atom|primary|unary)\w*|_?peek|_?advance|_?expect|_?next_token)\b|class\s+_?(Lexer|Tokenizer|Parser)\b'
