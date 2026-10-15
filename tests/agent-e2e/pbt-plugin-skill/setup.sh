#!/usr/bin/env bash
# A small Rust crate with an encode/decode round-trip pair and proptest already
# a dev-dependency. The property-based-testing plugin's skill should engage.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/src"
cat > "$WORK/Cargo.toml" <<'T'
[package]
name = "rle"
version = "0.1.0"
edition = "2021"

[dev-dependencies]
proptest = "1"
T
cat > "$WORK/src/lib.rs" <<'R'
/// Run-length encode bytes as (count, byte) pairs.
pub fn encode(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < input.len() {
        let b = input[i];
        let mut n = 1usize;
        while i + n < input.len() && input[i + n] == b && n < 255 {
            n += 1;
        }
        out.push(n as u8);
        out.push(b);
        i += n;
    }
    out
}

/// Inverse of `encode`.
pub fn decode(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for pair in input.chunks_exact(2) {
        out.extend(std::iter::repeat(pair[1]).take(pair[0] as usize));
    }
    out
}
R
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
