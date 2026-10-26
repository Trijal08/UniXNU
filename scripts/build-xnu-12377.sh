#!/usr/bin/env bash
set -euo pipefail
# Name the command that stops the build.
trap 'echo "build-xnu-12377.sh: line $LINENO: $BASH_COMMAND failed" >&2' ERR
# DEVELOPMENT BCM2837 kernel from xnu-12377 (iOS 26) and the patches/xnu-12377 series.
# The xnu-10063 kernel (build-xnu-4k-baseline.sh) and its trees are not touched: everything here lives under
# .cache/xnu-$XNU_VERSION-* and build/xnu-$XNU_VERSION. Source versions come from feeds.toml through the unixnu
# driver; the defaults are the macOS/iOS 26.5 release set.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
XNU_TAG=${UNIXNU_XNU_TAG:-xnu-12377.121.6}
XNU_COMMIT=${UNIXNU_XNU_COMMIT:-ac9718fb1af618d5ce8678d0dc6e8a58f252216f}
XNU_DARWIN=${UNIXNU_XNU_DARWIN:-25.5.0}
XNU_VERSION=${UNIXNU_XNU_VERSION:-12377.121.6}
LIBPLATFORM_TAG=${UNIXNU_LIBPLATFORM_TAG:-libplatform-375.120.2}
LIBDISPATCH_TAG=${UNIXNU_LIBDISPATCH_TAG:-libdispatch-1542.100.32}
AVAILABILITY_TAG=${UNIXNU_AVAILABILITYVERSIONS_TAG:-AvailabilityVersions-157.2}
# Kernel Debug Kit of the same release (macOS 26.5.2 25F84 = iOS 26.5.2 23F84), for the headers xnu-12377 takes from it.
KDK_URL=${UNIXNU_KDK_URL:-https://github.com/dortania/KdkSupportPkg/releases/download/25F84/Kernel_Debug_Kit_26.5.2_build_25F84.dmg}
KDK_SHA256=${UNIXNU_KDK_SHA256:-faef4393becf65c88a2791770904909babf1826d23d2ea478bf878a6417851ab}
IOS_MIN=${XNU_IOS_DEPLOYMENT_TARGET:-26.0}
JOBS=${JOBS:-4}
# XNU_KERNEL_ARCH: arm64 (default, no pointer signing in the kernel) or arm64e. It is a make
# command-line assignment, which overrides the environment, so it has to be threaded through here.
# arm64e is what turns on __has_feature(ptrauth_calls), and XNU compiles its arm64e userland
# support (shared-region JOP keys, the arm64e pager) only under that feature.
XNU_KERNEL_ARCH=${XNU_KERNEL_ARCH:-arm64}
PAC_DEFINE=""
if [ "$XNU_KERNEL_ARCH" = arm64e ]; then
  PAC_DEFINE="-DXNU_ARCH_PAC=1"
fi

WORK=.cache/xnu-$XNU_VERSION-work
FAKEROOT=$ROOT/.cache/xnu-$XNU_VERSION-fakeroot
KDKROOT=$ROOT/.cache/xnu-$XNU_VERSION-kdk
OUT=$ROOT/build/xnu-$XNU_VERSION
PATCHES=$ROOT/patches/xnu-12377

clone() {  # clone DIR REPOSITORY TAG
  if [ ! -d "$1/.git" ]; then
    git clone --depth 1 --branch "$3" "https://github.com/apple-oss-distributions/$2.git" "$1"
  fi
}

clone refer/xnu xnu "$XNU_TAG"
if [ "$(git -C refer/xnu rev-parse HEAD)" != "$XNU_COMMIT" ]; then
  echo "error: refer/xnu is not $XNU_TAG ($XNU_COMMIT); run unixnu fetch with the xnu-12377 feeds" >&2
  exit 1
fi
if [ ! -d "$WORK/.git" ]; then
  git clone --shared refer/xnu "$WORK"
fi
if [ "$(git -C "$WORK" rev-parse HEAD)" != "$XNU_COMMIT" ]; then
  echo "error: $WORK is not at $XNU_COMMIT; remove it to start again" >&2
  exit 1
fi
# Apply the series in name order; a patch that is already in the tree is skipped.
for PATCH in "$PATCHES"/*.patch; do
  if ! git -C "$WORK" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
    git -C "$WORK" apply --check "$PATCH"
    git -C "$WORK" apply "$PATCH"
  fi
done

if [ ! -x "$FAKEROOT/usr/local/libexec/availability.pl" ]; then
  clone refer/AvailabilityVersions AvailabilityVersions "$AVAILABILITY_TAG"
  mkdir -p "$FAKEROOT"
  make -C refer/AvailabilityVersions install -j"$JOBS" \
    OBJROOT="$OUT/availability" SYMROOT="$OUT/availability" \
    DSTROOT="$FAKEROOT" CMAKE="$(command -v cmake)" NINJA="$(command -v ninja)"
fi
export XNU_AVAILABILITY_PL=$FAKEROOT/usr/local/libexec/availability.pl

python3 scripts/kdk-headers.py --url "$KDK_URL" --sha256 "$KDK_SHA256" --cache .cache/kdk "$KDKROOT"

clone refer/libplatform libplatform "$LIBPLATFORM_TAG"
mkdir -p "$FAKEROOT/usr/local/include/kernel"
ditto refer/libplatform/include "$FAKEROOT/usr/local/include"
ditto refer/libplatform/private "$FAKEROOT/usr/local/include"
ditto refer/libplatform/private/platform "$FAKEROOT/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform"

clone refer/libdispatch libdispatch "$LIBDISPATCH_TAG"
python3 - "$FAKEROOT/usr/local/include/os" <<'PY'
from pathlib import Path
import subprocess, sys
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
for header in Path('refer/libdispatch/os').glob('*.h'):
    result = subprocess.run(['unifdef', '-DKERNEL=1', '-DOS_FIREHOSE_SPI=1',
                             '-DOS_VOUCHER_ACTIVITY_SPI_TYPES=1',
                             '-UOS_VOUCHER_ACTIVITY_SPI', str(header)], capture_output=True)
    if result.returncode not in (0, 1):
        raise SystemExit(result.stderr.decode())
    target = output / header.name
    if not target.exists() or target.read_bytes() != result.stdout:
        target.write_bytes(result.stdout)
PY

if [ ! -f "$FAKEROOT/.xnu-$XNU_VERSION-headers" ]; then
  make -C "$WORK" installhdrs SDKROOT=macosx ARCH_CONFIGS=ARM64 \
    MACHINE_CONFIGS=BCM2837 KERNEL_CONFIGS=DEVELOPMENT \
    RC_DARWIN_KERNEL_VERSION="$XNU_DARWIN" RC_ProjectSourceVersion="$XNU_VERSION" \
    MAKEJOBS=-j"$JOBS" OBJROOT="$OUT/hdrs-obj" SYMROOT="$OUT/hdrs-sym" DSTROOT="$FAKEROOT"
  touch "$FAKEROOT/.xnu-$XNU_VERSION-headers"
fi

# libfirehose_kernel.a for the kernel link (LD_KERNEL_ARCHIVES), arm64 like the kernel.
if [ ! -f "$FAKEROOT/usr/local/lib/kernel/libfirehose_kernel.a" ]; then
  if [ ! -d ".cache/libdispatch-$LIBDISPATCH_TAG-work/.git" ]; then
    git clone --shared refer/libdispatch ".cache/libdispatch-$LIBDISPATCH_TAG-work"
  fi
  python3 - ".cache/libdispatch-$LIBDISPATCH_TAG-work/xcodeconfig/libfirehose_kernel.xcconfig" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
s = s.replace('$(SDKROOT)/System/Library/Frameworks/Kernel.framework',
              '$(FAKEROOT_DIR)/System/Library/Frameworks/Kernel.framework')
s = s.replace('$(SDKROOT)/usr/local/include', '$(FAKEROOT_DIR)/usr/local/include')
lines = s.splitlines()
for index, line in enumerate(lines):
    if line.startswith('HEADER_SEARCH_PATHS =') and '$(SDKROOT)/usr/include' not in line:
        lines[index] += (' $(SDKROOT)/usr/include $(FAKEROOT_DIR)/usr/local/include'
                         ' $(XNU_SOURCE)/EXTERNAL_HEADERS')
s = '\n'.join(lines) + '\n'
if p.read_text() != s:
    p.write_text(s)
PY
  xcodebuild install -project ".cache/libdispatch-$LIBDISPATCH_TAG-work/libdispatch.xcodeproj" \
    -target libfirehose_kernel -configuration Release -sdk iphoneos -jobs "$JOBS" \
    ARCHS="$XNU_KERNEL_ARCH" VALID_ARCHS="$XNU_KERNEL_ARCH" ONLY_ACTIVE_ARCH=NO IPHONEOS_DEPLOYMENT_TARGET="$IOS_MIN" \
    CODE_SIGNING_ALLOWED=NO GCC_TREAT_WARNINGS_AS_ERRORS=NO \
    OBJROOT="$OUT/firehose/obj" SYMROOT="$OUT/firehose/sym" \
    DSTROOT="$FAKEROOT" FAKEROOT_DIR="$FAKEROOT" XNU_SOURCE="$ROOT/$WORK"
  if [ -f "$FAKEROOT/usr/local/lib/kernel/liblibfirehose_kernel.a" ]; then
    cp "$FAKEROOT/usr/local/lib/kernel/liblibfirehose_kernel.a" "$FAKEROOT/usr/local/lib/kernel/libfirehose_kernel.a"
  fi
  xcrun lipo -info "$FAKEROOT/usr/local/lib/kernel/libfirehose_kernel.a"
fi

EXTRA_CFLAGS=${XNU_EXTRA_CFLAGS:--Wno-reserved-identifier -Wno-pre-c11-compat -Wno-missing-designated-field-initializers -Wno-suggest-override -Wno-unnecessary-virtual-specifier -Wno-suggest-destructor-override -Wno-ms-bitfield-padding}
EXTRA_MAKE_ARGS=()
OBJ=$OUT/ios-obj/DEVELOPMENT_ARM64_BCM2837
SOURCE_CRYPTO=${XNU_SOURCE_CORECRYPTO14:-0}
if [ "$SOURCE_CRYPTO" != 0 ] && [ "$SOURCE_CRYPTO" != 1 ]; then
  echo 'XNU_SOURCE_CORECRYPTO14 must be 0 or 1' >&2
  exit 1
fi

# flag VARIABLE: the 0/1 value of a CONFIG_XNU_ option (0 when unset).
flag() {
  local value=${!1:-0}
  if [ "$value" != 0 ] && [ "$value" != 1 ]; then
    echo "$1 must be 0 or 1" >&2
    exit 1
  fi
  echo "$value"
}
# rebuild_on_change NAME VALUE OBJECT...: make does not track per-object flags, so drop the objects when VALUE changes.
rebuild_on_change() {
  local stamp=$OBJ/.$1 value=$2
  shift 2
  mkdir -p "$OBJ"
  if [ "$(cat "$stamp" 2>/dev/null || echo 0)" != "$value" ]; then
    for object in "$@"; do
      rm -f "$OBJ/$object"
    done
    printf '%s\n' "$value" > "$stamp"
  fi
}

# BCM2837 start.s routes the core timers through the Raspberry Pi local interrupt controller (physical 0x40000040)
# before the MMU is on; other boards need it off and use ArmCpu's GICv3 provider.
NATIVE_ARCH_TIMER=$(flag XNU_NATIVE_ARCH_TIMER)
EXTRA_MAKE_ARGS+=("start.o_SFLAGS_ADD=-DXNU_NATIVE_ARCH_TIMER=$NATIVE_ARCH_TIMER")
rebuild_on_change native-arch-timer-mode "$NATIVE_ARCH_TIMER" osfmk/DEVELOPMENT/start.o
# An EL1 port may have access to the virtual timer while physical-timer control traps.
VIRTUAL_TIMER_ONLY=$(flag XNU_VIRTUAL_TIMER_ONLY)
EXTRA_MAKE_ARGS+=("machine_routines.o_CFLAGS_ADD=-DXNU_VIRTUAL_TIMER_ONLY=$VIRTUAL_TIMER_ONLY")
rebuild_on_change virtual-timer-only-mode "$VIRTUAL_TIMER_ONLY" osfmk/DEVELOPMENT/machine_routines.o
# PAN on ARMv8.1+ cores; the files that read the PAN register by name need ARMv8.1 code generation.
NATIVE_PAN=$(flag XNU_NATIVE_PAN)
EXTRA_CFLAGS="$EXTRA_CFLAGS -DXNU_NATIVE_PAN=$NATIVE_PAN"
for object in copyio.o pcb.o platform_tests.o; do
  if [ "$NATIVE_PAN" = 1 ]; then
    EXTRA_MAKE_ARGS+=("${object}_CFLAGS_ADD=-march=armv8.1-a")
  fi
done
# Panic log in the device tree's pram region instead of the Raspberry Pi address PLATFORM_PANIC_LOG_PADDR.
DT_PANIC_REGION=$(flag XNU_DT_PANIC_REGION)
EXTRA_MAKE_ARGS+=("pe_init.o_CFLAGS_ADD=-DXNU_DT_PANIC_REGION=$DT_PANIC_REGION")
rebuild_on_change dt-panic-region-mode "$DT_PANIC_REGION" pexpert/DEVELOPMENT/pe_init.o
# trust_cache_init() at boot, which BCM2837 skips.
TRUST_CACHE_MODE=$(flag XNU_TRUST_CACHE_RUNTIME)
EXTRA_MAKE_ARGS+=("startup.o_CFLAGS_ADD=-DXNU_NATIVE_TRUST_CACHE=$TRUST_CACHE_MODE")
rebuild_on_change trust-cache-mode "$TRUST_CACHE_MODE" osfmk/DEVELOPMENT/startup.o
if [ "$SOURCE_CRYPTO" = 1 ]; then
  EXTRA_CFLAGS="$EXTRA_CFLAGS -DXNU_SOURCE_CORECRYPTO14=1"
fi
rebuild_on_change source-crypto-mode "$SOURCE_CRYPTO" osfmk/DEVELOPMENT/prng_random.o pexpert/DEVELOPMENT/pe_init.o
ARM_DT_PLATFORM=$(flag XNU_ARM_DT_PLATFORM)
ARMCPU=$(flag XNU_ARMCPU)
if [ "$ARMCPU" = 1 ] && { [ "$ARM_DT_PLATFORM" != 1 ] || [ "$NATIVE_ARCH_TIMER" != 1 ]; }; then
  echo 'ArmCpu requires ArmDTPlatform and native architectural timer routing' >&2
  exit 1
fi
EXTRA_CFLAGS="$EXTRA_CFLAGS -DXNU_ARM_DT_PLATFORM=$ARM_DT_PLATFORM -DXNU_ARMCPU=$ARMCPU"
rebuild_on_change platform-personalities "$ARM_DT_PLATFORM:$ARMCPU" iokit/DEVELOPMENT/KernelConfigTables.cpo
NATIVE_DWC3_CONSOLE=$(flag XNU_NATIVE_DWC3_CONSOLE)
SOURCE_PTHREAD=$(flag XNU_SOURCE_PTHREAD)
NATIVE_UFS_SM8350=$(flag XNU_NATIVE_UFS_SM8350)
EXTRA_CFLAGS="$EXTRA_CFLAGS -DXNU_NATIVE_UFS_SM8350=$NATIVE_UFS_SM8350"
EXTRA_CFLAGS="$EXTRA_CFLAGS -DXNU_NATIVE_DWC3_CONSOLE=$NATIVE_DWC3_CONSOLE"
rebuild_on_change native-dwc3-console "$NATIVE_DWC3_CONSOLE" pexpert/DEVELOPMENT/pe_serial.o osfmk/DEVELOPMENT/rtclock.o

export KERNEL_SDKROOT=$FAKEROOT
# The search paths xnu adds for SDK and KDK content this build does not have (iBoot and AppleFeatures headers,
# platform headers and libraries). Empty, they add nothing; missing, every compile warns -Wmissing-include-dirs.
mkdir -p "$FAKEROOT/usr/local/include/iBoot" \
  "$FAKEROOT/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/AppleFeatures" \
  "$KDKROOT/System/Library/Frameworks/Kernel.framework/Versions/A/Headers/platform" \
  "$FAKEROOT/usr/local/lib/kernel/platform" "$KDKROOT/usr/local/lib/kernel/platform"
# XNU_MAKE_KEEP_GOING=1 (make -k) compiles every object that can be compiled and reports all errors of one run.
MAKE_KEEP_GOING=()
if [ "$(flag XNU_MAKE_KEEP_GOING)" = 1 ]; then
  MAKE_KEEP_GOING=(-k)
fi
build_kernel() {
make ${MAKE_KEEP_GOING[@]+"${MAKE_KEEP_GOING[@]}"} -C "$WORK" "${XNU_BUILD_TARGET:-build}" SDKROOT="${XNU_SDKROOT:-iphoneos}" KDKROOT="$KDKROOT" \
  ARCH_CONFIGS=ARM64 MACHINE_CONFIGS=BCM2837 KERNEL_CONFIGS=DEVELOPMENT \
  ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG="${XNU_KERNEL_ARCH:-arm64}" LARGE_MEMORY_DEFINE=-UARM_LARGE_MEMORY \
  OTHER_CFLAGS="$PAC_DEFINE" \
  ARM64_LINK_ADDR=0xfffffff007004000 ARM64_PLKSEG_ADDR=0xfffffff004004000 \
  DEPLOYMENT_TARGET_FLAGS="-miphoneos-version-min=$IOS_MIN -DXNU_TARGET_OS_IOS" \
  DEPLOYMENT_LINKER_FLAGS="-Wl,-ios_version_min,$IOS_MIN" \
  RC_DARWIN_KERNEL_VERSION="$XNU_DARWIN" RC_ProjectSourceVersion="$XNU_VERSION" \
  BUILD_LTO=0 BOUND_CHECKS=0 BUILD_WERROR="${XNU_BUILD_WERROR:-0}" MAKEJOBS=-j"$JOBS" \
  CFLAGS_EXTRA="$EXTRA_CFLAGS" "${EXTRA_MAKE_ARGS[@]}" "$@" \
  OBJROOT="$OUT/ios-obj" SYMROOT="$OUT/ios-sym" DSTROOT="$OUT/ios-dst"
}
# XNU_EXTRA_LD_ARCHIVES: space-separated archives force-loaded into the kernel link, for the board's
# own link stubs (CONFIG_XNU_EXTRA_LD_ARCHIVES). A non-empty value also enables the archive link path,
# so a board that needs only stubs does not have to turn on an unrelated provider to get one.
EXTRA_LD_ARCHIVES=${XNU_EXTRA_LD_ARCHIVES:-}
EXTRA_LD_FLAGS=""
for archive in $EXTRA_LD_ARCHIVES; do
  if [ ! -f "$archive" ]; then
    echo "XNU_EXTRA_LD_ARCHIVES: $archive does not exist" >&2
    exit 1
  fi
  EXTRA_LD_FLAGS="$EXTRA_LD_FLAGS $archive"
done
if [ -n "$EXTRA_LD_ARCHIVES" ] || [ "$SOURCE_CRYPTO" = 1 ] || [ "$ARM_DT_PLATFORM" = 1 ] || [ "$ARMCPU" = 1 ] || [ "$NATIVE_DWC3_CONSOLE" = 1 ] || [ "$SOURCE_PTHREAD" = 1 ] || [ "$NATIVE_UFS_SM8350" = 1 ]; then
  # First produce the unlinked objects/filelist for collision discovery.
  # Bootstrap references are enabled only after the provider archive exists.
  rm -f "$OBJ/osfmk/DEVELOPMENT/prng_random.o" "$OBJ/pexpert/DEVELOPMENT/pe_init.o"
  rm -f "$OBJ/pexpert/DEVELOPMENT/pe_serial.o" "$OBJ/osfmk/DEVELOPMENT/rtclock.o"
  build_kernel CFLAGS_EXTRA="$EXTRA_CFLAGS -UXNU_SOURCE_CORECRYPTO14 -UXNU_ARM_DT_PLATFORM -UXNU_ARMCPU -UXNU_NATIVE_DWC3_CONSOLE" \
    "LD_KERNEL_ARCHIVES=-L$FAKEROOT/usr/local/lib/kernel -lfirehose_kernel$EXTRA_LD_FLAGS"
  ARCHIVE_FLAGS="-L$FAKEROOT/usr/local/lib/kernel -lfirehose_kernel$EXTRA_LD_FLAGS"
  ARCHIVE_DEPENDENCIES="$EXTRA_LD_ARCHIVES"
  if [ "$SOURCE_CRYPTO" = 1 ]; then
    python3 scripts/build-corecrypto-14.py --xnu-12377
    python3 scripts/build-corecrypto-14.py --xnu-12377 --namespace
    CRYPTO_ARCHIVE=$ROOT/build/corecrypto-12377/libcorecrypto_kernel_noasm-namespaced.a
    ARCHIVE_FLAGS="$ARCHIVE_FLAGS $CRYPTO_ARCHIVE"
    ARCHIVE_DEPENDENCIES="$CRYPTO_ARCHIVE"
  fi
  for component in ArmDTPlatform ArmCpu NativeDwc3Console; do
    if { [ "$component" = ArmDTPlatform ] && [ "$ARM_DT_PLATFORM" = 1 ]; } || \
       { [ "$component" = ArmCpu ] && [ "$ARMCPU" = 1 ]; } || \
       { [ "$component" = NativeDwc3Console ] && [ "$NATIVE_DWC3_CONSOLE" = 1 ]; }; then
      python3 scripts/build-platform-12377.py "$component"
      archive=$ROOT/build/platform-12377/$component/lib$component.a
      ARCHIVE_FLAGS="$ARCHIVE_FLAGS -Wl,-force_load,$archive"
      ARCHIVE_DEPENDENCIES="$ARCHIVE_DEPENDENCIES $archive"
    fi
  done
  if [ "$SOURCE_PTHREAD" = 1 ]; then
    clone refer/libpthread-539 libpthread libpthread-539.100.4
    python3 scripts/build-pthread-12377.py
    archive=$ROOT/build/libpthread-12377/libpthread_kernel.a
    ARCHIVE_FLAGS="$ARCHIVE_FLAGS -Wl,-force_load,$archive"
    ARCHIVE_DEPENDENCIES="$ARCHIVE_DEPENDENCIES $archive"
  fi
  if [ "$NATIVE_UFS_SM8350" = 1 ]; then
    python3 scripts/build-storage-12377.py
    archive=$ROOT/build/storage-12377/libStorageUfs.a
    ARCHIVE_FLAGS="$ARCHIVE_FLAGS -Wl,-force_load,$archive"
    ARCHIVE_DEPENDENCIES="$ARCHIVE_DEPENDENCIES $archive"
  fi
  rm -f "$OBJ/osfmk/DEVELOPMENT/prng_random.o" "$OBJ/pexpert/DEVELOPMENT/pe_init.o"
  rm -f "$OBJ/iokit/DEVELOPMENT/KernelConfigTables.cpo"
  rm -f "$OBJ/pexpert/DEVELOPMENT/pe_serial.o" "$OBJ/osfmk/DEVELOPMENT/rtclock.o"
  build_kernel "LD_KERNEL_ARCHIVES=$ARCHIVE_FLAGS" "XNU_EXTERNAL_KERNEL_ARCHIVES=$ARCHIVE_DEPENDENCIES"
  if [ "$SOURCE_CRYPTO" = 1 ]; then
    python3 scripts/seal-source-crypto-kernel.py --static-if-defaults "$OBJ/mach.development.bcm2837.unstripped.noctf"
  fi
else
  build_kernel "LD_KERNEL_ARCHIVES=-L$FAKEROOT/usr/local/lib/kernel -lfirehose_kernel$EXTRA_LD_FLAGS"
fi
# The iPhoneOS kernel is mach.development.bcm2837[.unstripped[.noctf]] in the object directory; ios-sym may not exist.
for directory in "$OUT/ios-sym" "$OBJ"; do
  if [ -d "$directory" ]; then
    find "$directory" -maxdepth 1 \( -name 'kernel.*' -o -name 'mach.*' \) -type f -exec ls -l {} +
  fi
done
