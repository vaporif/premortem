{
  description = "slopmortem — find similar dead startups, write per-candidate post-mortems";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };

      python = pkgs.python313;

      workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      pythonSet = (pkgs.callPackage pyproject-nix.build.packages {inherit python;})
        .overrideScope (
        pkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
        ]
      );

      runtimeLibs = with pkgs;
        [
          stdenv.cc.cc.lib
          zlib
          openssl
          libxml2
          libxslt
        ]
        ++ pkgs.lib.optionals pkgs.stdenv.isLinux [
          glibc
        ];
    in {
      packages.default = pythonSet.mkVirtualEnv "slopmortem-env" workspace.deps.default;

      apps.default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/slopmortem";
      };

      devShells.default = pkgs.mkShell {
        name = "slopmortem";

        # ``python`` (Nix's CPython) is intentionally *not* in this list.
        # The dev workflow goes ``uv venv`` → ``uv sync`` → ``uv run`` and uv
        # provisions its own standalone CPython under ~/.local/share/uv/python/.
        # Reasoning: PyPI wheels (especially the native ones — ``tokenizers``,
        # ``onnxruntime``) are built against PSF's standard CPython ABI and
        # silently abort when loaded into Nix's CPython. The flake's
        # ``packages.default`` (uv2nix mkVirtualEnv) still uses Nix Python and
        # is fine for ``nix run .`` consumers, but the dev path swaps in uv's
        # Python so PyPI wheels load cleanly.
        packages = with pkgs; [
          uv
          ruff
          basedpyright

          just

          pre-commit
          taplo

          claude-code

          nodejs_25

          docker
          docker-compose

          yq-go
          jq
          git
          git-lfs
          curl
          zip
        ];

        env = {
          # Let uv download + manage Python for the .venv so PyPI wheels
          # (especially ``tokenizers``/``onnxruntime``) match the ABI they
          # were built against. ``UV_PYTHON`` pins the version uv resolves to;
          # uv pulls a python-build-standalone tarball if it's not cached.
          UV_PYTHON = "3.13";
          UV_PYTHON_DOWNLOADS = "automatic";
          UV_PROJECT_ENVIRONMENT = ".venv";

          HF_HOME = "./data/hf";
          SENTENCE_TRANSFORMERS_HOME = "./data/hf";
        };

        shellHook = ''
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}:''${LD_LIBRARY_PATH:-}"

          # ``uv venv`` (no --python pin) honors UV_PYTHON. uv downloads + caches
          # its standalone CPython on first invocation; subsequent shells reuse it.
          if [ ! -d .venv ]; then
            echo "→ creating .venv via uv (uv-managed Python)"
            uv venv
          fi

          if [ -f pyproject.toml ]; then
            uv sync --frozen 2>/dev/null || uv sync
          fi
        '';
      };
    });
}
