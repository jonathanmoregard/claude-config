#!/usr/bin/env bash
# Squint test: blurred copies of a screen at 5, 10 and 20 px next to the original.
# The primary element should still dominate in every copy.
# Usage: squint.sh <screen.png> [out-dir]
set -euo pipefail
src=$1
out=${2:-$(dirname "$src")/squint}
mkdir -p "$out"
base=$(basename "$src" .png)
for r in 5 10 20; do
  magick "$src" -blur "0x$r" "$out/$base--blur$r.png"
done
echo "$out/$base--blur{5,10,20}.png"
