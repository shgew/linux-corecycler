{
  # Force FULL evaluation of the NixOS module (options, assertions, every mkIf
  # path) against a minimal config without building the system closure. Plain
  # `nix flake check` only proves the module evaluates as a definition, not that
  # it instantiates. `builtins.seq` on drvPath throws on failure while storing a
  # context-free string, so nothing is realized.
  nixosModuleCheck =
    {
      nixpkgs,
      system,
      module,
      config ? { },
      overlays ? [ ],
    }:
    let
      pkgs = nixpkgs.legacyPackages.${system};
      sys = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          module
          config
          (
            { lib, ... }:
            {
              nixpkgs.overlays = overlays;
              boot.isContainer = true;
              networking.useHostResolvConf = lib.mkForce false;
              system.stateVersion = lib.trivial.release;
            }
          )
        ];
      };
    in
    pkgs.runCommand "module-eval" {
      ok = builtins.seq sys.config.system.build.toplevel.drvPath "instantiated";
    } ''echo "$ok" > "$out"'';

  # Eval-only gate for a package whose closure is unfree or uncached and so is
  # not built here. Same idiom as the module check.
  drvEvalCheck =
    {
      pkgs,
      name ? "drv-eval",
      drv,
    }:
    if !(pkgs.lib.meta.availableOn pkgs.stdenv.hostPlatform drv) then
      pkgs.runCommand name { }
        ''echo "skipped: ${name} not available on ${pkgs.stdenv.hostPlatform.system}" > "$out"''
    else
      pkgs.runCommand name {
        ok = builtins.seq drv.drvPath "evaluated";
      } ''echo "$ok" > "$out"'';

  # Inspects the BUILT output, not the pyproject declaration: a flat top-level
  # module collides with any other application in a merged site-packages.
  pythonSitePackagesCheck =
    {
      pkgs,
      drv,
      package,
      name ? "python-site-packages",
    }:
    pkgs.runCommand name { } ''
      shopt -s nullglob dotglob
      spdirs=("${drv}"/lib/python*/site-packages)
      if [ "''${#spdirs[@]}" -ne 1 ]; then
        echo "expected exactly one site-packages under ${drv}/lib, found ''${#spdirs[@]}"
        exit 1
      fi
      sp="''${spdirs[0]}"
      if [ ! -d "$sp/${package}" ]; then
        echo "declared package '${package}' missing from site-packages"
        exit 1
      fi
      bad=0
      for entry in "$sp"/*; do
        base="''${entry##*/}"
        case "$base" in
        "${package}" | *.dist-info) ;;
        *)
          echo "flat top-level entry in site-packages: $base"
          bad=1
          ;;
        esac
      done
      if [ "$bad" -ne 0 ]; then
        echo "every module must live under the '${package}' package"
        exit 1
      fi
      echo "ok: single top-level package '${package}'" > "$out"
    '';
}
