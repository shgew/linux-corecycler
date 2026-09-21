{
  lib,
  stdenv,
  kernel,
  llvmPackages_latest,
}:
let
  kernelUsesLLVM = builtins.any (
    flag:
    builtins.match ".*LLVM=1.*" (toString flag) != null
    || builtins.match ".*CC=clang.*" (toString flag) != null
  ) (kernel.makeFlags or [ ]);
in
{
  inherit kernelUsesLLVM;
  buildStdenv = if kernelUsesLLVM then llvmPackages_latest.stdenv else stdenv;
  nativeBuildInputs =
    kernel.moduleBuildDependencies
    ++ lib.optionals kernelUsesLLVM [
      llvmPackages_latest.lld
    ];
  makeFlags = lib.optionals kernelUsesLLVM [
    "LLVM=1"
    "CC=clang"
    "LD=ld.lld"
  ];
}
