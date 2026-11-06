{
  description = "visual-verify: DPR=2 screenshot capture with matched python + playwright + browsers (works on NixOS without uv)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          playwright
          pyyaml
        ]);
      in {
        packages.default = pkgs.writeShellApplication {
          name = "visual-verify";
          runtimeInputs = [ pythonEnv ];
          # Pin PLAYWRIGHT_BROWSERS_PATH to the nix-managed driver — versions of
          # playwright and browsers are matched inside a single nixpkgs pin, so
          # no "browser not found" / libglib mismatch (the failure mode SKILL.md
          # documents for uv-run on NixOS).
          text = ''
            export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
            # bin/screenshot's shebang is `uv run --script`; bypass it by calling
            # python directly with the script path.
            exec python3 ${./bin/screenshot} "$@"
          '';
        };

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/visual-verify";
        };

        # Optional: `nix develop` gives an interactive shell with the same env
        # (useful for iterating on bin/screenshot or ad-hoc python REPL).
        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv ];
          shellHook = ''
            export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
            echo "visual-verify devshell — python + playwright + browsers pinned"
          '';
        };
      });
}
