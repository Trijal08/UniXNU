#!/usr/bin/env python3
"""Build public libpthread 539 against XNU 12377 headers and its versioned shims."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from xnu_symbols import kernel_names
SOURCE = Path(os.environ.get('UNIXNU_REFER_12377', ROOT / '.cache/refer-12377')) / 'libpthread-539'
REVISION = '1f4f5265b319111142f1bf3a27d4484ef5a98314'
if subprocess.check_output(['git', '-C', SOURCE, 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
    raise SystemExit('Unexpected libpthread source revision')
if subprocess.check_output(['git', '-C', SOURCE, 'status', '--porcelain'], text=True):
    raise SystemExit('The libpthread reference must remain immutable')
WORK = ROOT / '.cache/libpthread-12377-work'
if not WORK.exists():
    subprocess.run(['git', 'clone', '--shared', SOURCE, WORK], check=True)
if subprocess.check_output(['git', '-C', WORK, 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
    raise SystemExit('Unexpected libpthread work-tree revision')
for patch in sorted((ROOT / 'patches/libpthread').glob('*.patch')):
    if subprocess.run(['git', '-C', WORK, 'apply', '--reverse', '--check', patch],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        subprocess.run(['git', '-C', WORK, 'apply', patch], check=True)
SOURCE = WORK
OUT = ROOT / 'build/libpthread-12377'
OUT.mkdir(parents=True, exist_ok=True)
object_directory = ROOT / 'build/xnu-12377.121.6/ios-obj/DEVELOPMENT_ARM64_BCM2837'
recipes = list(object_directory.rglob('pthread_shims.o.json'))
if len(recipes) != 1:
    raise SystemExit(f'Expected one compiled BSD pthread-shim recipe, found {recipes}')
recipe = recipes[0]
record = json.loads(recipe.read_text().strip().rstrip(','))
sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()


def compile_one(source, destination, namespace=None):
    if source.name == 'source-pthread-12377.c':
        command = []
        arguments = record['arguments']
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value == record['file']:
                command.append(str(source))
            elif value == '-o':
                command.extend(['-o', str(destination)])
                index += 1
            else:
                command.append(value)
            index += 1
    else:
        framework = ROOT / '.cache/xnu-12377.121.6-fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A'
        command = ['xcrun', '--sdk', 'iphoneos', 'clang', '-target', 'arm64-apple-ios26.0',
                   '-isysroot', sdk, '-mkernel', '-ffreestanding', '-fno-builtin', '-fno-stack-check',
                   '-std=gnu11', '-fblocks', '-O2', '-mgeneral-regs-only',
                   '-DKERNEL=1', '-DKERNEL_PRIVATE=1', '-DXNU_KERNEL_PRIVATE=1',
                   '-DMACH_KERNEL_PRIVATE=1', '-DMACH_ASSERT=1', '-DDEVELOPMENT=1', '-DXNU_TARGET_OS_IOS=1',
                   '-D__PTHREAD_EXPOSE_INTERNALS__=1', '-DABSOLUTETIME_SCALAR_TYPE=1', '-DNEEDS_SCHED_CALL_T=1',
                   '-I', str(SOURCE / 'private'), '-I', str(SOURCE / 'include'), '-I', str(SOURCE),
                   '-I', str(framework / 'PrivateHeaders'), '-I', str(framework / 'Headers'),
                   '-Wno-deprecated-declarations', '-c', str(source), '-o', str(destination)]
    if namespace:
        command += ['-include', str(namespace)]
    result = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    destination.with_suffix('.log').write_text(result.stdout)
    if result.returncode:
        raise SystemExit(f'{source.name}: compilation failed\n{result.stdout}')
    data = destination.read_bytes()
    if struct.unpack_from('<4I', data) != (0xfeedfacf, 0x100000c, 0, 1):
        raise SystemExit('Unexpected pthread object architecture')
    offset = 32
    deployment = None
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        cmd, length = struct.unpack_from('<II', data, offset)
        if cmd == 0x32:
            deployment = struct.unpack_from('<II', data, offset + 8)
        offset += length
    if deployment != (2, 0x1a0000):
        raise SystemExit(f'Unexpected pthread deployment: {deployment}')


def names(path, mode, global_only=True):
    result = subprocess.check_output(['xcrun', 'nm', *(['-g'] if global_only else []), mode, '-j', path], text=True)
    return {line.strip() for line in result.splitlines() if line.startswith('_')}


sources = [SOURCE / 'kern' / name for name in ('kern_init.c', 'kern_support.c', 'kern_synch.c')]
objects = []
for source in sources:
    destination = OUT / (source.stem + '.o')
    compile_one(source, destination)
    objects.append(destination)
kernel_names = kernel_names(ROOT, global_only=False, object_directory=object_directory)
definitions = set().union(*(names(path, '-U') for path in objects))
collisions = definitions & kernel_names
if collisions:
    namespace = OUT / 'namespace.h'
    namespace.write_text(''.join(f'#define {name[1:]} pthread539_{name[1:]}\n' for name in sorted(collisions)))
    for source, destination in zip(sources, objects):
        compile_one(source, destination, namespace)
definitions = set().union(*(names(path, '-U') for path in objects))
imports = set().union(*(names(path, '-u') for path in objects)) - definitions
# The component is statically linked: os_log's DSO identity is the real
# executable Mach-O header, synthesized by the Mach-O executable linker.
missing = sorted(imports - kernel_names - {'___dso_handle'})
report = {'revision': REVISION, 'sources': 3, 'ordinary_arm64_ios26_audited': True,
          'namespace_collisions_isolated': sorted(collisions), 'missing_kernel_names': missing,
          'linker_synthesized_symbols': ['___dso_handle'],
          'registration_observed': False}
(OUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
if not {'_pthread_kern', '_current_uthread'} <= collisions:
    raise SystemExit('Missing expected pthread/XNU static symbol isolation')
if missing:
    raise SystemExit(f'Unavailable real kernel imports: {missing}')
archive = OUT / 'libpthread_kernel.a'
archive.unlink(missing_ok=True)
compile_one(ROOT / 'scripts/source-pthread-12377.c', OUT / 'source_pthread_bootstrap.o')
bootstrap_missing = names(OUT / 'source_pthread_bootstrap.o', '-u') - definitions - kernel_names - {'___dso_handle'}
if bootstrap_missing:
    raise SystemExit(f'Unavailable bootstrap imports: {sorted(bootstrap_missing)}')
subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects, OUT / 'source_pthread_bootstrap.o'], check=True)
print('Source libpthread 539: 3 real C sources + bootstrap against XNU 12377; ARM64/iOS 26 audited; no unavailable kernel names')
