#!/usr/bin/env python3
"""Build the generic platform and CPU drivers with the current XNU 12377 ABI."""
import argparse
import json
import re
from pathlib import Path
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('component', choices=['ArmDTPlatform', 'ArmCpu', 'NativeDwc3Console'])
args = parser.parse_args()
obj = ROOT / 'build/xnu-12377.121.6/ios-obj/DEVELOPMENT_ARM64_BCM2837'
recipe = 'pexpert/DEVELOPMENT/pe_serial.o.json' if args.component == 'NativeDwc3Console' else 'iokit/DEVELOPMENT/IOPlatformExpert.cpo.json'
record = json.loads((obj / recipe).read_text().strip().rstrip(','))
out = ROOT / 'build/platform-12377' / args.component
out.mkdir(parents=True, exist_ok=True)
source = ROOT / 'scripts/native-dwc3-console.c' if args.component == 'NativeDwc3Console' else ROOT / 'drivers' / args.component / (args.component + '.cpp')
if args.component == 'ArmCpu':
    patched = out / 'ArmCpu.cpp'
    patched.write_bytes(source.read_bytes())
    for patch_name in ('ArmCpu-arm64-assembly.patch', 'ArmCpu-12377-boot-interrupt.patch'):
        driver_patch = ROOT / 'patches/drivers' / patch_name
        subprocess.run(['git', 'apply', '--directory', str(out.relative_to(ROOT)), driver_patch], cwd=ROOT, check=True)
    source = patched
# Darwin's ARM64 assembler treats ';' as a comment: use explicit newlines.
for assembly in re.findall(r'__asm__\s+volatile\s*\(\s*"((?:\\.|[^"\\])*)"', source.read_text()):
    if ';' in assembly:
        raise SystemExit('Semicolon in inline assembly would discard following ARM64 instructions')
destination = out / (args.component + '.o')
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
command += ['-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-Wno-c99-designator',
            '-Wno-reorder-init-list', '-Wno-deprecated-enum-enum-conversion',
            '-Wno-unnecessary-virtual-specifier']
(out / 'compile-command.json').write_text(json.dumps(command, indent=2) + '\n')
result = subprocess.run(command, cwd=record['directory'], capture_output=True, text=True)
(out / 'compile.log').write_text(result.stdout + result.stderr)
if result.returncode:
    raise SystemExit(result.stdout + result.stderr)
data = destination.read_bytes()
if struct.unpack_from('<4I', data) != (0xfeedfacf, 0x100000c, 0, 1):
    raise SystemExit('Expected a plain ARM64 Mach-O object')
cursor = 32
deployment = None
for _ in range(struct.unpack_from('<I', data, 16)[0]):
    kind, size = struct.unpack_from('<II', data, cursor)
    if kind == 0x32:
        deployment = struct.unpack_from('<II', data, cursor + 8)
    cursor += size
if deployment != (2, 26 << 16):
    raise SystemExit(f'Expected iOS 26 deployment, found {deployment}')
if args.component == 'NativeDwc3Console':
    cursor = 32
    checked = 0
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        kind, size = struct.unpack_from('<II', data, cursor)
        if kind == 0x19:
            for index in range(struct.unpack_from('<I', data, cursor + 64)[0]):
                section = cursor + 72 + index * 80
                if data[section:section+16].rstrip(b'\0') == b'__text':
                    length, offset = struct.unpack_from('<QI', data, section + 40)
                    words = struct.unpack_from('<' + 'I' * (length // 4), data, offset)
                    for index, word in enumerate(words):
                        if word == 0xd5033fdf:
                            if index + 1 == len(words) or words[index + 1] & 0xffffffe0 != 0xd53be020:
                                raise SystemExit('Native USB timeout ISB lacks its emitted CNTPCT_EL0 read')
                            checked += 1
        cursor += size
    if not checked:
        raise SystemExit('No native USB counter sequences found')
    (out / 'counter-audit.json').write_text(json.dumps({'isb_counter_pairs_verified': checked}) + '\n')
archive = out / ('lib' + args.component + '.a')
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, destination], check=True)
print(f'{args.component}: compiled against XNU 12377, ARM64/iOS 26 object verified')
