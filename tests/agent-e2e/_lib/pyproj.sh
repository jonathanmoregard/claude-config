#!/usr/bin/env bash
# Small Python project in $E2E_WORK whose deps come from a Nix devShell, the
# way projects on this machine provide them. Usage: pyproj.sh PKG
# Callers add their own module files, then run commit_all.
set -euo pipefail
WORK="${E2E_WORK:?}"
PKG="${1:?package name}"
mkdir -p "$WORK/$PKG" "$WORK/tests"
cat > "$WORK/flake.nix" <<EOF
{
  description = "$PKG";
  inputs.nixpkgs.url = "nixpkgs";
  outputs = { self, nixpkgs }:
    let pkgs = nixpkgs.legacyPackages.x86_64-linux;
    in {
      devShells.x86_64-linux.default = pkgs.mkShell {
        packages = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
      };
    };
}
EOF
cat > "$WORK/pyproject.toml" <<EOF
[project]
name = "$PKG"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF
cat > "$WORK/README.md" <<EOF
# $PKG

Dev shell: \`nix develop\` (Python + pytest). Run tests with \`nix develop -c pytest\`.
EOF
: > "$WORK/$PKG/__init__.py"
