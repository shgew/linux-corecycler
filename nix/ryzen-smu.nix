# ryzen_smu kernel module for Curve Optimizer, PBO controls, and PM table access.
# The local patch fixes two pinned-upstream safety defects: non-OK mailbox
# responses being lost while retries remain, and stale SMN data after read failure.
# Supports GCC and Clang/LLVM kernels through the shared toolchain helper.
# Source: https://github.com/amkillam/ryzen_smu
{
  lib,
  stdenv,
  fetchFromGitHub,
  kernel,
  llvmPackages_latest,
}:
let
  toolchain = import ./kernel-module-toolchain.nix {
    inherit
      lib
      stdenv
      kernel
      llvmPackages_latest
      ;
  };

  version = "0.1.7-unstable-2026-08-15";

  src = fetchFromGitHub {
    owner = "amkillam";
    repo = "ryzen_smu";
    rev = "d2983668300dd2a598e5a7dc40e71ce0678cc270";
    hash = "sha256-OmEoycRO3hGkqueLa0i6AzmwMEbdkkPrwJkMyYxOTek=";
  };
in
toolchain.buildStdenv.mkDerivation {
  pname = "ryzen-smu-${kernel.version}";
  inherit version src;
  patches = [ ./ryzen-smu-mailbox.patch ];

  hardeningDisable = [ "pic" ];

  nativeBuildInputs = toolchain.nativeBuildInputs;

  makeFlags = [
    "TARGET=${kernel.modDirVersion}"
    "KERNEL_BUILD=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
  ]
  ++ toolchain.makeFlags
  ++ lib.optionals toolchain.kernelUsesLLVM [
    "KCFLAGS=-Wno-unused-command-line-argument"
  ];

  installPhase = ''
    runHook preInstall
    install ryzen_smu.ko -Dm444 -t $out/lib/modules/${kernel.modDirVersion}/kernel/drivers/ryzen_smu
    runHook postInstall
  '';

  meta = {
    description = "Linux kernel driver that exposes access to the SMU for AMD Ryzen Processors";
    homepage = "https://github.com/amkillam/ryzen_smu";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
