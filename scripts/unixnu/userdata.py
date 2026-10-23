"""Userdata payload: the raw image for the device's `userdata` partition.

It holds the unchanged OS and SystemOS disk images of the IPSW, byte for byte, behind a small manifest
(the SHNGROOT layout the native UFS root driver reads):

    0x000000  manifest block (4096 bytes), zero padding to 1 MiB
    0x100000  window "OS"        the whole attached OS disk image, 1 MiB aligned
    ....      window "SystemOS"  the whole attached SystemOS disk image, 1 MiB aligned

Manifest (little endian): "SHNGROOT", u32 version=1, u32 count, u64 image_bytes, then `count` 128-byte entries
{char name[32], u64 offset, u64 size, u32 block_size=512, u32 flags(1=read-only), u8 sha256[32], 40 reserved},
and at byte 4064 the SHA-256 of bytes 0..4063.

The images come from the IPSW (a local file or a remote URL, see ipsw.py) and are checked against the pins of
board.toml. The disk images are attached read-only without mounting (macOS hdiutil). Nothing is flashed: the
report prints the fastboot command, to be run by the user.
"""
import hashlib
import json
import plistlib
import re
import struct
import subprocess

from .ipsw import Ipsw
from .udif import open_image, UdifError
import shutil
from .util import Error, log, run, sha256_file, tool

MIB = 1 << 20
CHUNK = 8 * MIB


def paths(ctx):
    out = ctx.work / 'userdata'
    return out, out / 'userdata-root.img', out / 'userdata-root.json'


def image_size(dmg):
    info = subprocess.run(['hdiutil', 'imageinfo', '-plist', str(dmg)], capture_output=True, check=True).stdout
    return plistlib.loads(info)['Size Information']['Total Bytes']


def attach(dmg):
    """Attach read-only without mounting; return the whole-disk device."""
    out = subprocess.run(['hdiutil', 'attach', '-nomount', '-readonly', '-noverify', '-noautofsck', str(dmg)],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        match = re.match(r'(/dev/disk\d+)\s', line)
        if match:
            return match.group(1)
    raise Error('could not find the attached disk device')


def build(ctx, dry_run=False):
    log('userdata payload')
    spec = ctx.board.get('userdata')
    if not spec:
        raise Error('board.toml has no [userdata]')
    native = shutil.which('hdiutil') is not None
    if not native:
        log('userdata: no hdiutil here, the disk images are expanded by the UDIF/raw reader (scripts/unixnu/udif.py)')
    source = Ipsw(ctx)
    capacity = (spec['last_lba'] - spec['first_lba'] + 1) * spec.get('lba_bytes', 4096)
    if dry_run:
        for window in spec['windows']:
            name, size, _ = source.info(window['component'])
            log(f'userdata: {window["name"]} would be extracted from {name} ({size >> 20} MiB)')
        log('userdata: dry run, nothing extracted or written')
        return None
    if native:
        sources = [(window['name'], source.extract(window['component'])) for window in spec['windows']]
    else:
        sources = [(window['name'], source.open_member(window['component'])) for window in spec['windows']]
    layout, cursor = [], MIB
    images = {}
    for name, dmg in sources:
        if native:
            size = image_size(dmg)
        else:
            try:
                images[name] = open_image(dmg)
            except UdifError as error:
                raise Error(f'{name}: {error}') from None
            size = images[name].size
        layout.append((name, cursor, size))
        cursor += (size + MIB - 1) // MIB * MIB
    total = cursor
    log(f'userdata: image {total} bytes ({total / 2**30:.2f} GiB) in a partition of {capacity} bytes')
    if total > capacity:
        raise Error('the image is larger than the userdata partition')
    for name, dmg in sources:
        log(f'userdata: {name} from {getattr(dmg, "name", source.member([w for w in spec["windows"] if w["name"] == name][0]["component"]))}')
    out, image, report = paths(ctx)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    with image.open('wb') as target:
        target.truncate(total)
        for (name, offset, size), (_, dmg) in zip(layout, sources):
            if not native:
                digest = hashlib.sha256()
                target.seek(offset)
                written = 0

                def sink(data, _digest=digest):
                    nonlocal written
                    _digest.update(data)
                    target.write(data)
                    written += len(data)

                last = [0]

                def progress(done, count, position):
                    if position - last[0] >= 512 * MIB or done == count:
                        last[0] = position
                        log(f'userdata: {name} {position >> 20} / {size >> 20} MiB ({done}/{count} chunks)')

                try:
                    images[name].expand(sink, progress)
                except UdifError as error:
                    raise Error(f'{name}: {error}') from None
                if written != size:
                    raise Error(f'{name}: expanded {written} bytes, expected {size}')
                entries.append((name, offset, size, digest.digest()))
                log(f'userdata: {name} {size} bytes at {offset:#x} sha256 {digest.hexdigest()}')
                continue
            device = attach(dmg)
            try:
                raw = device.replace('/dev/disk', '/dev/rdisk')
                digest = hashlib.sha256()
                target.seek(offset)
                done = 0
                with open(raw, 'rb', buffering=0) as handle:
                    while done < size:
                        want = min(CHUNK, size - done)
                        data = handle.read(want)
                        if len(data) != want:
                            raise Error(f'short read from {raw} at {done}')
                        digest.update(data)
                        target.write(data)
                        done += want
            finally:
                subprocess.run(['hdiutil', 'detach', device], capture_output=True)
            entries.append((name, offset, size, digest.digest()))
            log(f'userdata: {name} {size} bytes at {offset:#x} sha256 {digest.hexdigest()}')
        header = bytearray(4096)
        header[0:8] = b'SHNGROOT'
        struct.pack_into('<IIQ', header, 8, 1, len(entries), total)
        for index, (name, offset, size, digest) in enumerate(entries):
            struct.pack_into('<32sQQII32s40s', header, 24 + 128 * index, name.encode(), offset, size, 512, 1, digest, bytes(40))
        header[4064:4096] = hashlib.sha256(bytes(header[:4064])).digest()
        target.seek(0)
        target.write(header)
    data = {'image_bytes': total, 'image_sha256': sha256_file(image), 'manifest_sha256': hashlib.sha256(bytes(header)).hexdigest(),
            'windows': [{'name': n, 'offset': o, 'size': s, 'sha256': d.hex()} for n, o, s, d in entries],
            'userdata_bytes': capacity, 'flash_command': f'fastboot flash {spec.get("partition", "userdata")} {image}',
            'source': 'remote' if source.remote else 'local', 'hardware_tested': False}
    report.write_text(json.dumps(data, indent=2) + '\n')
    log(f'userdata: {image}')
    log(f'flash it yourself with: {data["flash_command"]}')
    return image
