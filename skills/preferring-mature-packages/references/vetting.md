# Vetting commands per ecosystem

Run the checks for the ecosystem the project actually uses. A clean result is
weak evidence; a flagged one is a hard stop or warning per SKILL.md step 3.

## Nix (nixpkgs packages, any language)

One-shot meta check for any attribute (`python3Packages.<name>`, `<tool>`, …):

```bash
nix eval --json nixpkgs#python3Packages.<name>.meta --apply 'm: {
  broken = m.broken or false;
  vulns = m.knownVulnerabilities or [];
  maintainers = map (x: x.github or x.name) (m.maintainers or []);
  teams = map (t: t.shortName) (m.teams or []); }'
```

- `vulns` non-empty or `broken: true` → hard stop. Evaluation refuses such a
  package ("Refusing to evaluate package … marked as insecure"); that refusal
  is the answer, not an obstacle to route around.
- `maintainers` and `teams` both empty → maintainerless in nixpkgs: warn, then
  judge upstream health separately (a healthy upstream can be maintainerless
  here).
- `knownVulnerabilities` is not exhaustive: its absence proves little.
- Prefer a nixpkgs package over a third-party flake. Adding a flake input
  anyway: `nix flake metadata <flake-ref>` shows its input graph; add
  `inputs.<name>.inputs.nixpkgs.follows = "nixpkgs";` to avoid a second
  nixpkgs.

## Rust

- `cargo deny check advisories` — works without a `deny.toml`; fails on
  RustSec vulnerabilities **and** `unmaintained` advisories.
- `cargo audit --deny warnings` — same database; plain `cargo audit` only
  warns on `unmaintained`.
- Shared audits as a positive signal: crates other organisations have
  reviewed.
  ```bash
  cargo vet init
  cargo vet import mozilla; cargo vet import google; cargo vet import bytecode-alliance
  cargo vet regenerate exemptions && cargo vet   # "N fully audited"
  ```
  Crates no longer listed under `[[exemptions.*]]` in
  `supply-chain/config.toml` are covered by an imported audit.
- All three tools are in nixpkgs: `nix shell nixpkgs#cargo-deny nixpkgs#cargo-audit nixpkgs#cargo-vet`.

## Python

- In a Nix project the package comes from nixpkgs: run the Nix meta check on
  `python3Packages.<name>` first.
- pip/uv projects: `pip-audit --no-deps --disable-pip -r requirements.txt`
  with every requirement pinned (`pkg==x.y.z`). Plain `pip-audit -r` resolves
  and installs like `pip install`, running build scripts. pip-audit finds known
  vulnerabilities only, never malicious packages.
  - nixpkgs' `pip-audit` needs a Python with pip: set
    `PIPAPI_PYTHON_LOCATION=<venv>/bin/python` (any `python3 -m venv`).
- Signals to read on the PyPI project page (no command): Trusted Publishing /
  PEP 740 attestations (provenance, not trust — a *changed* trusted publisher
  is a takeover flag); wheels for the project's CPython version.
