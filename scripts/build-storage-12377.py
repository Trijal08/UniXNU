#!/usr/bin/env python3
"""Build public IOStorageFamily's native kernel path and SM8350 UFS for XNU 12377."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from xnu_symbols import kernel_names

SOURCE = ROOT / 'refer/IOStorageFamily'
REVISION = '7edb88fbae296fb7c8ce2f64e115e116e566d51c'
if subprocess.check_output(['git', '-C', SOURCE, 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
    raise SystemExit('Expected pinned IOStorageFamily 337.100.1')
if subprocess.check_output(['git', '-C', SOURCE, 'status', '--porcelain'], text=True):
    raise SystemExit('IOStorageFamily reference must remain immutable')
# --host picks the SoC glue, exactly as build-arm-driver.py does for the xnu-10063 build: sm8350
# applies zeta's source patch and defines NATIVE_UFS_SM8350, exynos uses the ExynosUfs* headers that
# already live in the driver and needs no patch. --root-partition names the GPT partition holding
# the root image (userdata on sheng, darwin on komodo).
import argparse
_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument('--host', choices=['sm8350', 'exynos'], default='sm8350')
_parser.add_argument('--root-partition', metavar='NAME')
_args = _parser.parse_args()

OUT = ROOT / 'build/storage-12377'
OUT.mkdir(parents=True, exist_ok=True)
UFS = OUT / 'NativeUfs'
shutil.copytree(ROOT / 'drivers/NativeUfs', UFS,
                ignore=shutil.ignore_patterns('.git', 'build'), dirs_exist_ok=True)
# The assembled tree is nested inside the checkout. git apply can otherwise
# silently skip paths outside that repository-relative cwd prefix. Apply from
# a fresh non-repository directory to the explicit exported source path, then
# prove the complete patch is present before compiling anything.
if _args.host == 'sm8350':
    patch = ROOT / 'patches/drivers/NativeUfs-sm8350.patch'
    with tempfile.TemporaryDirectory(prefix='unixnu-ufs-patch-') as temporary:
        command = ['git', 'apply', '--unsafe-paths', '--directory', str(UFS)]
        subprocess.run(command + [str(patch)], cwd=temporary, check=True)
        subprocess.run(command + ['--reverse', '--check', str(patch)], cwd=temporary, check=True)
    print('Storage 12377: complete SM8350 source patch verified', flush=True)
else:
    print('Storage 12377: Exynos host glue, no source patch needed', flush=True)
# The IOKit/storage headers must come from the same pinned IOStorageFamily as the sources, not from
# build/storage-abi, which prepare-storage-headers.py generates for the xnu-10063 kernel from a
# different revision. Mixing them gives "use of undeclared identifier" on members such as
# _processShutdownThread. The sources are flat here, so they are republished under IOKit/storage.
ABI = OUT / 'abi/include/IOKit/storage'
ABI.mkdir(parents=True, exist_ok=True)
for header in sorted(SOURCE.glob('*.h')):
    shutil.copy(header, ABI / header.name)
# TargetConditionals.h is an SDK header the storage sources include; the xnu-10063 ABI directory
# publishes it at its include root, so this one does too rather than depending on the host sysroot.
_target_conditionals = ROOT / 'scripts/linux/TargetConditionals.h'
if not _target_conditionals.is_file():
    _target_conditionals = ROOT / 'build/storage-abi/include/TargetConditionals.h'
if _target_conditionals.is_file():
    shutil.copy(_target_conditionals, ABI.parent.parent / 'TargetConditionals.h')
else:
    raise SystemExit('no TargetConditionals.h to publish for the storage build')
print(f'Storage 12377: {len(list(ABI.glob("*.h")))} IOKit/storage headers from the pinned source',
      flush=True)

OBJ = ROOT / 'build/xnu-12377.121.6/ios-obj/DEVELOPMENT_ARM64_BCM2837'
record = json.loads((OBJ / 'iokit/DEVELOPMENT/IOPlatformExpert.cpo.json').read_text().strip().rstrip(','))

# DriverKit is a separate user-driver backend requiring generated IIG RPC
# classes. This build links native kernel block devices. Keep the public
# reference immutable and retain the complete ordinary block-storage path.
driver = (SOURCE / 'IOBlockStorageDriver.cpp').read_text()
old = '#include "IOUserBlockStorageDevice_kext.h"'
assert driver.count(old) == 1
driver = driver.replace(old, '#if !IOSTORAGE_NATIVE_ONLY\n' + old + '\n#endif')
old = '''    _userBlockStorageDevice = OSDynamicCast(IOUserBlockStorageDevice, provider);

    if (_userBlockStorageDevice) {
        _perfControlClient = _userBlockStorageDevice->getPerfControlClient();
        assert(_perfControlClient != NULL);
    }
    else {'''
assert driver.count(old) == 1
driver = driver.replace(old, '''#if IOSTORAGE_NATIVE_ONLY
    _userBlockStorageDevice = NULL;
#else
''' + old[:-1] + '''
#endif
    {''')
patched = OUT / 'IOBlockStorageDriver.cpp'
patched.write_text(driver)
sources = [SOURCE / (name + '.cpp') for name in (
    'IOStorage', 'IOBlockStorageDevice', 'IOMedia', 'IOMediaBSDClient',
    'IOPartitionScheme', 'IOGUIDPartitionScheme', 'IOFDiskPartitionScheme',
    'IOApplePartitionScheme', 'IOAppleLabelScheme', 'IOFilterScheme')]
sources += [patched, UFS / 'NativeUfs.cpp']
verified = os.environ.get('XNU_NATIVE_UFS_VERIFIED_ROOT') == '1'
extra = []
if verified:
    anchor = ROOT / 'scripts/zeta-owner-root-key.h'
    if not anchor.is_file():
        raise SystemExit('Verified UFS root requires the independent public owner anchor')
    extra = ['-DNATIVE_VERIFIED_BLOCK=1', '-DNATIVE_UFS_POLICY_FROM_DISK=1',
             '-I', str(ROOT / 'kits/VerifiedRoot/include'),
             '-I', str(ROOT / 'drivers/NativeVirtioBlock')]
objects = []
def compile_one(source, record):
    destination = OUT / (source.stem + '.o')
    command = []
    arguments = record['arguments']
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == record['file']:
            command.append(str(source))
        elif value == '-o':
            command += ['-o', str(destination)]
            index += 1
        elif value in ('-MF', '-MJ', '-MT', '-MQ'):
            index += 1
        elif value == '-Xclang' and arguments[index + 1].startswith('-W'):
            index += 1
        elif value not in ('-MD', '-MMD', '-MP') and not value.startswith('-W'):
            command.append(value)
        index += 1
    command += extra + ['-I', str(OUT / 'abi/include'), '-I', str(SOURCE),
                '-I', str(UFS),
                *(['-DNATIVE_UFS_SM8350=1'] if _args.host == 'sm8350'
                  else ['-DNATIVE_UFS_HOST_EXYNOS=1']),
                *(['-DNATIVE_UFS_ROOT_PARTITION="%s"' % _args.root_partition]
                  if _args.root_partition else []),
                '-DIOSTORAGE_NATIVE_ONLY=1',
                '-DNATIVE_UFS_PROGRESS_OBSERVER=0',
                '-Wall', '-Wextra', '-Wno-unused-parameter', '-Wno-deprecated-declarations',
                '-Wno-c99-designator', '-Wno-reorder-init-list']
    destination.with_suffix('.command.json').write_text(json.dumps(command, indent=2) + '\n')
    result = subprocess.run(command, cwd=record['directory'], capture_output=True, text=True)
    destination.with_suffix('.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit(f'{source.name}: compilation failed\n{result.stdout}{result.stderr}')
    data = destination.read_bytes()
    # MH_MAGIC_64, CPU_TYPE_ARM64, MH_OBJECT, subtype ARM64_ALL (0) or ARM64E (2). The top byte is
    # CPU_SUBTYPE_MASK, where arm64e carries its pointer-auth ABI version, so arm64e reads
    # 0x80000002. An arm64e kernel can only link arm64e archives.
    magic, cputype, cpusubtype, filetype = struct.unpack_from('<4I', data)
    if (magic, cputype, filetype) != (0xfeedfacf, 0x100000c, 1) or (cpusubtype & ~0xff000000) not in (0, 2):
        raise SystemExit(f'{source.name}: expected an ARM64 or ARM64E object, got '
                         f'magic {magic:#x} cputype {cputype:#x} subtype {cpusubtype} filetype {filetype}')
    at, deployment = 32, None
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        kind, size = struct.unpack_from('<II', data, at)
        if kind == 0x32:
            deployment = struct.unpack_from('<II', data, at + 8)
        at += size
    if deployment != (2, 26 << 16):
        raise SystemExit(f'{source.name}: unexpected deployment {deployment}')
    objects.append(destination)
    print(f'Storage 12377: compiled {source.name}', flush=True)

for source in sources:
    compile_one(source, record)
if verified:
    recipes = list(OBJ.rglob('pthread_shims.o.json'))
    if len(recipes) != 1:
        raise SystemExit('Expected the compiled BSD recipe for owner verification')
    bsd = json.loads(recipes[0].read_text().strip().rstrip(','))
    anchor_source = OUT / 'zeta_owner_root.c'
    anchor_source.write_text('#include "verified_root.h"\n#include "' + str(anchor) + '"\n')
    for source in (ROOT / 'kits/VerifiedRoot/src/verified_root.c',
                   ROOT / 'kits/VerifiedRoot/adapters/xnu_crypto.c', anchor_source):
        compile_one(source, bsd)

def names(path, mode):
    return {s.strip() for s in subprocess.check_output(
        ['xcrun', 'nm', '-g', mode, '-j', path], text=True).splitlines() if s.startswith('_')}

defined = set().union(*(names(p, '-U') for p in objects))
imports = set().union(*(names(p, '-u') for p in objects)) - defined
kernel = kernel_names(ROOT, object_directory=OBJ)
collisions = sorted(defined & kernel)
missing = sorted(imports - kernel - {'___dso_handle'})
report = {'source_revision': REVISION, 'objects': len(objects),
          'ordinary_arm64_ios26_audited': True, 'native_kernel_backend_only': True,
          'sm8350_stream_id': 0xe0, 'kernel_collisions': collisions,
          'missing_kernel_imports': missing, 'hardware_qualified': False, 'owner_policy_from_ufs': verified, 'progress_observer_compiled': False,
          'native_driver_sha256': hashlib.sha256(patched.read_bytes()).hexdigest()}
(OUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
if collisions or missing:
    raise SystemExit(f'Storage symbol audit: collisions={collisions}; missing={missing}')
archive = OUT / 'libStorageUfs.a'
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects], check=True)
print('Storage 12377: native IOStorageFamily + SM8350 UFS; no unavailable imports')
