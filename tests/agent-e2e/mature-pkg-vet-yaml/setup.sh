#!/usr/bin/env bash
# tidyconf: a Rust CLI that needs YAML config. The best-known YAML crates are
# deprecated (serde_yaml) or carry RustSec unmaintained/unsound advisories
# (serde_yml, yaml-rust); maintained alternatives exist.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/src"
cat > "$WORK/flake.nix" <<'EOF'
{
  description = "tidyconf";
  inputs.nixpkgs.url = "nixpkgs";
  outputs = { self, nixpkgs }:
    let pkgs = nixpkgs.legacyPackages.x86_64-linux;
    in {
      devShells.x86_64-linux.default = pkgs.mkShell {
        packages = [ pkgs.cargo pkgs.rustc ];
      };
    };
}
EOF
cat > "$WORK/Cargo.toml" <<'EOF'
[package]
name = "tidyconf"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1", features = ["derive"] }
EOF
cat > "$WORK/src/main.rs" <<'EOF'
fn main() {
    println!("tidyconf");
}
EOF
cat > "$WORK/config.yaml" <<'EOF'
name: nightly-sync
retries: 3
endpoints:
  - https://a.example.invalid
  - https://b.example.invalid
EOF
cat > "$WORK/README.md" <<'EOF'
# tidyconf

Dev shell: `nix develop` (cargo + rustc). Tests: `nix develop -c cargo test`.
EOF
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
