{
  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen on Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } (
      let
        systems = [
          "x86_64-linux"
          "aarch64-linux"
        ];

        # The sandbox strips .git, so setuptools-scm cannot read the tag. The
        # version is the release line pyproject declares plus the commit the
        # flake was evaluated from, the same shape setuptools-scm's
        # node-and-date scheme emits. Both the store path and the wheel's
        # METADATA carry it, so an installed build names its own commit.
        scmVersion =
          let
            inherit (inputs) self;
            base = (inputs.nixpkgs.lib.importTOML ./pyproject.toml).tool.setuptools_scm.fallback_version;
            node =
              if self ? shortRev then
                "g${self.shortRev}"
              else if self ? dirtyShortRev then
                "g${inputs.nixpkgs.lib.removeSuffix "-dirty" self.dirtyShortRev}.dirty"
              else
                "d${builtins.substring 0 8 self.lastModifiedDate}";
          in
          "${base}+${node}";

        # Shared builder for a system: the FOSS `default` and the `full` (mprime,
        # unfree) variants from one mkCoreCycler. Used by both perSystem (default
        # -> a built check) and flake.packages (full -> an off-CI eval gate).
        # Keyed on a package set, so the overlay can build against the consumer's
        # nixpkgs instead of handing back a build against the one pinned here.
        buildWith =
          pkgs:
          let

            # Default python3 (not a pinned minor): Hydra only builds/caches
            # pyside6 for the default interpreter, so pinning python312 forced a
            # ~50-min from-source pyside6 build on every CI run. python3 keeps
            # the heavy Qt bindings a cache.nixos.org hit. requires-python in
            # pyproject.toml still allows >=3.12 for downstream users.
            python = pkgs.python3;
            pythonPkgs = python.pkgs;

            # Shared build function - backends list is the only difference
            mkCoreCycler =
              {
                backends ? [
                  pkgs.stress-ng
                  pkgs.stressapptest
                ],
                pnameSuffix ? "",
              }:
              pythonPkgs.buildPythonApplication {
                pname = "corecycler${pnameSuffix}";
                version = scmVersion;
                pyproject = true;

                src = ./.;

                build-system = [
                  pythonPkgs.setuptools
                  pythonPkgs.setuptools-scm
                ];

                env.SETUPTOOLS_SCM_PRETEND_VERSION = scmVersion;

                dependencies = [
                  pythonPkgs.pyside6
                ];

                # The full unit/property suite gates the BUILD (offscreen Qt,
                # HOME in the sandbox tmpdir). The e2e subprocess replays
                # ("slow") stay outside the sandbox: they exercise systemd-run
                # scopes + wall-clock polling and belong to the dev loop, not
                # the gate.
                # pytest-xdist is not optional: pyproject's addopts are
                # `-n auto`, so the sandboxed check needs the plugin to run at
                # all. It is also what keeps that check near 15s.
                nativeCheckInputs = [
                  pythonPkgs.pytestCheckHook
                  pythonPkgs.hypothesis
                  pythonPkgs.pytest-cov
                  pythonPkgs.pytest-timeout
                  pythonPkgs.pytest-xdist
                ];
                doCheck = true;
                preCheck = ''
                  export QT_QPA_PLATFORM=offscreen
                  export HOME=$TMPDIR
                '';
                disabledTestMarks = [ "slow" ];

                # The package build runs the non-slow tests without coverage.
                # The standalone coverage check below enforces the 100% floor.
                pytestFlags = [ ];

                # Qt6 runtime needs
                nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
                buildInputs = [ pkgs.qt6.qtbase ];

                dontWrapQtApps = true;
                preFixup = ''
                  makeWrapperArgs+=("''${qtWrapperArgs[@]}")
                '';

                # Install icon, desktop file, and asset SVGs
                postInstall = ''
                  install -Dm644 assets/icon.svg $out/share/icons/hicolor/scalable/apps/corecycler.svg
                  install -Dm644 assets/corecycler.desktop $out/share/applications/corecycler.desktop
                  install -d $out/share/corecycler/assets
                  install -Dm644 assets/*.svg $out/share/corecycler/assets/
                '';

                # Make stress test backends available on PATH at runtime
                postFixup = ''
                  wrapProgram $out/bin/corecycler \
                    --prefix PATH : ${
                      pkgs.lib.makeBinPath (
                        backends
                        ++ [
                          pkgs.util-linux # for setpriv (containment payload lifetime)
                          pkgs.dmidecode # for DIMM info in Memory tab
                          pkgs.libnotify # for notify-send desktop notifications
                        ]
                      )
                    }
                '';

                meta = {
                  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen";
                  license = pkgs.lib.licenses.gpl3Plus;
                  mainProgram = "corecycler";
                  platforms = pkgs.lib.platforms.linux;
                };
              };
          in
          {
            inherit pkgs;
            # FOSS-only: stress-ng + stressapptest (no unfree software).
            default = mkCoreCycler { };
            # Full: includes mprime (unfree, fetched from a flaky external mirror,
            # x86_64-only) and y-cruncher (unfree, x86_64-only). meta.platforms
            # reflects that so the standard's drvEvalCheck skips `full` on aarch64,
            # while the FOSS `default` still builds on both arches.
            full =
              (mkCoreCycler {
                backends = [
                  pkgs.mprime
                  pkgs.y-cruncher
                  pkgs.stress-ng
                  pkgs.stressapptest
                ];
                pnameSuffix = "-full";
              }).overrideAttrs
                (o: {
                  meta = o.meta // {
                    platforms = [ "x86_64-linux" ];
                  };
                });
          };

        glueOverlay = final: _prev: {
          linux-corecycler = (buildWith final).default;
          linux-corecycler-full = (buildWith final).full;
        };
        fixOverlays = [ ];

        buildFor =
          system:
          let
            pkgs = import inputs.nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [ inputs.self.overlays.default ];
            };
          in
          {
            inherit pkgs;
            default = pkgs.linux-corecycler;
            full = pkgs.linux-corecycler-full;
          };
      in
      {
        inherit systems;

        flake = {
          # NixOS module - kernel modules, device access, udev rules, package
          nixosModules.default = import ./nix/module.nix { inherit (inputs) self; };

          overlays = {
            default = inputs.nixpkgs.lib.composeManyExtensions ([ glueOverlay ] ++ fixOverlays);
            probe = glueOverlay;
          };

          # OFF-CI exception (standard README "declared == built"): `full` pulls
          # mprime -- unfree, and fetched from an external mirror that does not
          # build reliably on a free runner. Expose it as a real `nix build .#full`
          # target via flake.packages (NOT perSystem.packages, which base aliases
          # into BUILT checks); CI eval-gates it with drvEvalCheck below instead of
          # realizing the unfree mprime closure.
          packages = builtins.listToAttrs (
            map (system: {
              name = system;
              value.full = (buildFor system).full;
            }) systems
          );
        };

        perSystem =
          { system, pkgs, ... }:
          let
            b = buildFor system;
            checksLib = import ./nix/checks.nix;
          in
          {
            # The FOSS default uses stress-ng and stressapptest.
            packages.default = b.default;

            # The package's own build environment (interpreter, PySide6, pytest,
            # hypothesis and pytest-cov, so `python -m pytest` runs straight from
            # `nix develop`.
            formatter = pkgs.nixfmt;

            devShells.default = pkgs.mkShell {
              inputsFrom = [ b.default ];
              packages = [
                pkgs.just
                pkgs.nixfmt
                pkgs.ruff
              ];
              CORECYCLER_DEV_SHELL = "1";
            };

            checks = {
              default = b.default;
              ruff = pkgs.runCommand "corecycler-ruff" { nativeBuildInputs = [ pkgs.ruff ]; } ''
                export RUFF_CACHE_DIR=$TMPDIR/ruff-cache
                ruff check ${inputs.self}/src
                touch "$out"
              '';
              ruff-format = pkgs.runCommand "corecycler-ruff-format" { nativeBuildInputs = [ pkgs.ruff ]; } ''
                export RUFF_CACHE_DIR=$TMPDIR/ruff-cache
                ruff format --check ${inputs.self}/src
                touch "$out"
              '';
              nixfmt = pkgs.runCommand "corecycler-nixfmt" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
                nixfmt --check ${inputs.self}/flake.nix ${inputs.self}/nix/*.nix
                touch "$out"
              '';
              coverage = b.default.overrideAttrs (old: {
                pname = "corecycler-coverage";
                pytestFlags = [
                  "--cov=corecycler"
                  "--cov-report=term-missing"
                  "--cov-fail-under=100"
                ];
              });

              # Eval-only gate for the off-CI `full`: force its full build graph to
              # EVALUATE (catching dep/version/unfree breakage) without realizing the
              # uncached, unfree mprime closure. The real build happens off-CI.
              full-eval = checksLib.drvEvalCheck {
                pkgs = inputs.nixpkgs.legacyPackages.${system};
                name = "corecycler-full-eval";
                drv = b.full;
              };

              # Built-output ground truth: the wheel ships exactly one top-level
              # package -- a flat module (cli.py) collides with any other app in a
              # merged site-packages. tests/test_packaging.py is the fast pytest
              # mirror of the same invariant.
              python-site-packages = checksLib.pythonSitePackagesCheck {
                inherit (b) pkgs;
                drv = b.default;
                package = "corecycler";
              };

              # Force full evaluation of the NixOS module (options + assertions +
              # every mkIf path) without building the closure.
              module-eval-nixos = checksLib.nixosModuleCheck {
                inherit (inputs) nixpkgs;
                inherit system;
                module = import ./nix/module.nix { inherit (inputs) self; };
                config = {
                  nixpkgs.config.allowUnfree = true; # mprime backend is unfree
                  services.corecycler = {
                    enable = true;
                    deviceAccessUser = "corecycler-test";
                  };
                  # the eval fixture must declare the user the module grants access to
                  users.users.corecycler-test = {
                    isSystemUser = true;
                    group = "corecycler-test";
                  };
                  users.groups.corecycler-test = { };
                };
              };
            }
            // inputs.nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
              user-containment = import ./nix/containment-test.nix {
                inherit (b) pkgs;
                corecyclerModule = import ./nix/module.nix { inherit (inputs) self; };
              };

              # The out-of-tree modules compile against the user's own kernel, so an
              # upstream header move is a user-visible FTBFS nothing here would catch.
              # Both ends of the range nixpkgs offers are built on purpose.
              kernel-modules =
                let
                  sources = {
                    ryzen-smu = ./nix/ryzen-smu.nix;
                    zenpower = ./nix/zenpower.nix;
                    it87 = ./nix/it87.nix;
                  };
                  forKernel =
                    kernel:
                    inputs.nixpkgs.lib.mapAttrs' (
                      name: f:
                      inputs.nixpkgs.lib.nameValuePair "${name}-${kernel.version}" (
                        b.pkgs.callPackage f { inherit kernel; }
                      )
                    ) sources;
                in
                b.pkgs.linkFarm "corecycler-kernel-modules" (
                  forKernel b.pkgs.linuxPackages.kernel // forKernel b.pkgs.linuxPackages_latest.kernel
                );
            };
          };
      }
    );
}
