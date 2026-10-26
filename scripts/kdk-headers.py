#!/usr/bin/env python3
"""Make a headers-only KDKROOT for xnu-12377 from a Kernel Debug Kit disk image.

  kdk-headers.py --dmg KDK.dmg | --url URL --sha256 HEX [--cache DIR]  OUTPUT

xnu-12377 includes CoreEntitlements/V2, TrustCache, CodeSignature and TrustedExecutionMonitor headers that are not in
its source; Apple ships them in the KDK (KDK_SDK.pkg: Kernel.framework/PrivateHeaders/platform, KDK.pkg:
System/Library/KernelSupport). OUTPUT gets only those headers, in the KDK layout, so KDKROOT=OUTPUT adds them to the
include path and nothing else: the KDK archives (libTightbeam.a, KernelSupport lib<SoC>.os.<config>.a) stay out of the
link. The disk image is read with hdiutil on macOS and 7z elsewhere; the pkg (xar), its pbzx payloads and their odc
cpio archives are read here. Nothing from the KDK is written to Git.
"""
import argparse
import hashlib
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path

PLATFORM = 'System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform/'
WANTED = {
    'KDK_SDK.pkg/Payload': tuple(PLATFORM + name + '/' for name in
                                 ('CoreEntitlements', 'TrustCache', 'CodeSignature', 'TrustedExecutionMonitor')),
    'KDK.pkg/Payload': ('System/Library/KernelSupport/CoreEntitlements/',),
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(url, wanted, cache):
    path = cache / url.rsplit('/', 1)[-1]
    if path.is_file() and sha256(path) == wanted:
        return path
    cache.mkdir(parents=True, exist_ok=True)
    print(f'kdk-headers: download {url}', flush=True)
    partial = path.with_suffix('.part')
    with urllib.request.urlopen(url, timeout=60) as response, open(partial, 'wb') as out:
        shutil.copyfileobj(response, out, 1 << 20)
    if sha256(partial) != wanted:
        partial.unlink()
        sys.exit(f'{url}: sha256 does not match {wanted}')
    partial.rename(path)
    return path


def copy_pkg(dmg, work):
    """KernelDebugKit.pkg of the disk image, copied to work."""
    target = work / 'KernelDebugKit.pkg'
    if sys.platform == 'darwin':
        mount = work / 'mnt'
        mount.mkdir()
        subprocess.run(['hdiutil', 'attach', '-nobrowse', '-readonly', '-mountpoint', mount, dmg], check=True,
                       stdout=subprocess.DEVNULL)
        try:
            shutil.copyfile(next(mount.rglob('KernelDebugKit.pkg')), target)
        finally:
            subprocess.run(['hdiutil', 'detach', mount], check=True, stdout=subprocess.DEVNULL)
    else:
        subprocess.run(['7z', 'e', '-y', f'-o{work}', dmg, '*/KernelDebugKit.pkg', '-r'], check=True,
                       stdout=subprocess.DEVNULL)
    if not target.is_file():
        sys.exit(f'{dmg}: no KernelDebugKit.pkg')
    return target


def xar_entries(pkg):
    """{path: (heap offset, length, encoding)} of the files of a xar archive."""
    with open(pkg, 'rb') as handle:
        magic, header_size, _, toc_packed, _, _ = struct.unpack('>4sHHQQI', handle.read(28))
        if magic != b'xar!':
            sys.exit(f'{pkg}: not a xar archive')
        handle.seek(header_size)
        toc = ElementTree.fromstring(zlib.decompress(handle.read(toc_packed)))
    heap = header_size + toc_packed
    entries = {}

    def walk(node, prefix):
        for item in node.findall('file'):
            path = prefix + item.findtext('name')
            data = item.find('data')
            if data is not None:
                encoding = data.find('encoding').get('style')
                entries[path] = (heap + int(data.findtext('offset')), int(data.findtext('length')), encoding)
            walk(item, path + '/')

    walk(toc.find('toc'), '')
    return entries


def pbzx_chunks(handle, length):
    """Decoded bytes of a pbzx stream of the given length, chunk by chunk."""
    end = handle.tell() + length
    if handle.read(4) != b'pbzx':
        sys.exit('payload is not pbzx')
    handle.read(8)
    while handle.tell() < end:
        _, size = struct.unpack('>QQ', handle.read(16))
        chunk = handle.read(size)
        yield lzma.decompress(chunk) if chunk.startswith(b'\xfd7zXZ\x00') else chunk


class Stream:
    """Exact-size reads over an iterator of byte chunks."""

    def __init__(self, chunks):
        self.chunks, self.buffer = chunks, bytearray()

    def read(self, size):
        while len(self.buffer) < size:
            chunk = next(self.chunks, None)
            if chunk is None:
                sys.exit('payload ends inside a cpio record')
            self.buffer += chunk
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


def extract_cpio(stream, prefixes, output):
    """Write the regular files of an odc cpio archive whose path starts with one of prefixes; returns their count."""
    count = 0
    while True:
        header = stream.read(76)
        if header[:6] != b'070707':
            sys.exit('payload is not an odc cpio archive')
        mode = int(header[18:24], 8)
        name_size, file_size = int(header[59:65], 8), int(header[65:76], 8)
        name = stream.read(name_size)[:-1].decode()
        data = stream.read(file_size)
        if name == 'TRAILER!!!':
            return count
        name = name.removeprefix('./')
        if mode & 0o170000 == 0o100000 and name.startswith(prefixes):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            count += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--dmg', type=Path)
    source.add_argument('--url')
    parser.add_argument('--sha256', help='required with --url, checked for --dmg when given')
    parser.add_argument('--cache', type=Path, default=Path('.cache/kdk'))
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.url and not args.sha256:
        parser.error('--url needs --sha256')
    dmg = fetch(args.url, args.sha256, args.cache) if args.url else args.dmg
    if args.dmg and args.sha256 and sha256(dmg) != args.sha256:
        sys.exit(f'{dmg}: sha256 does not match {args.sha256}')
    stamp = args.output / '.kdk-headers'
    identity = f'{dmg.name}\n'
    if stamp.is_file() and stamp.read_text() == identity:
        return
    if args.output.exists():
        shutil.rmtree(args.output)
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
        work = Path(temporary)
        pkg = copy_pkg(dmg.resolve(), work)
        entries = xar_entries(pkg)
        with open(pkg, 'rb') as handle:
            for payload, prefixes in WANTED.items():
                if payload not in entries:
                    sys.exit(f'{dmg}: {payload} missing from KernelDebugKit.pkg')
                offset, length, encoding = entries[payload]
                if encoding != 'application/octet-stream':
                    sys.exit(f'{payload}: unexpected xar encoding {encoding}')
                handle.seek(offset)
                count = extract_cpio(Stream(pbzx_chunks(handle, length)), prefixes, args.output)
                if not count:
                    sys.exit(f'{payload}: none of {prefixes} found')
                print(f'kdk-headers: {payload}: {count} headers', flush=True)
    stamp.write_text(identity)


if __name__ == '__main__':
    main()
