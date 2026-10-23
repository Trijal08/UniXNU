"""Owner-verified root policies, built from the root image on any host.

`unixnu build policies` signs every window of the root image (userdata-root.img, see userdata.py) with the owner
key and verifies the result with the kit's C provider, the way scripts/build-owner-os-policy.py does on macOS from
an hdiutil attachment. Each window gets build/verified-root-<window>/ in the build tree: manifest.bin (the signed
policy), trusted-key.bin (the public anchor), verification.json and original-samples.bin, which is what
scripts/build-verified-root-probe.py (a [kernel] archive) turns into the archive the kernel links with
CONFIG_XNU_VERIFIED_ROOT_PROBE=y and the verified NativeUfs matches against the window it finds on the medium.

The owner key is generated here when it does not exist yet (RSA-3072, build/verified-root-host/keys/owner.pem,
mode 0600). It never leaves the build tree; the kernel carries only the public anchor.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import uuid

from . import userdata
from .util import Error, log, run

BLOCK = 4096
POLICY_DIRS = {'OS': 'os', 'SystemOS': 'systemos'}


class _Window:
    """A read-only stream over [offset, offset + size) of a file, buffered for the 4 KiB reads of manifest.make."""

    def __init__(self, path, offset, size, buffer_bytes=16 << 20):
        self.file = path.open('rb')
        self.offset, self.size = offset, size
        self.position = 0
        self.buffer, self.buffer_start = b'', 0
        self.buffer_bytes = buffer_bytes

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.position
        n = min(n, self.size - self.position)
        out = bytearray()
        while n:
            if not (self.buffer_start <= self.position < self.buffer_start + len(self.buffer)):
                self.file.seek(self.offset + self.position)
                self.buffer = self.file.read(min(self.buffer_bytes, self.size - self.position))
                self.buffer_start = self.position
                if not self.buffer:
                    raise Error(f'short read of the root image at {self.offset + self.position:#x}')
            start = self.position - self.buffer_start
            piece = self.buffer[start:start + n]
            out += piece
            self.position += len(piece)
            n -= len(piece)
        return bytes(out)

    def seek(self, position):
        self.position = position

    def close(self):
        self.file.close()


def _manifest_module(ctx):
    path = ctx.root / 'kits/VerifiedRoot/tools/manifest.py'
    spec = importlib.util.spec_from_file_location('verified_manifest', path)
    module = importlib.util.module_from_spec(spec)
    bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # no __pycache__ in the kit submodule
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = bytecode
    return module


def ensure_keys(keys):
    """The owner key pair; generated once, private key readable by the owner only."""
    private, public = keys / 'owner.pem', keys / 'owner-public.pem'
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(keys, 0o700)
    if not private.is_file():
        log('policies: generating the owner key (RSA-3072) in ' + str(keys))
        fd = os.open(private, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', private], capture=True)
    os.chmod(private, 0o600)
    run(['openssl', 'pkey', '-in', private, '-pubout', '-out', public], capture=True)
    return private, public


def host_library(ctx, host):
    """The kit's C verifier with the OpenSSL provider, as a shared library the host Python loads."""
    library = host / ('verified-root.dylib' if sys.platform == 'darwin' else 'verified-root.so')
    sources = [ctx.root / 'kits/VerifiedRoot/src/verified_root.c', ctx.root / 'kits/VerifiedRoot/tests/openssl_provider.c']
    if library.is_file() and all(library.stat().st_mtime >= s.stat().st_mtime for s in sources):
        return library
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if not compiler:
        raise Error('no host C compiler (cc/clang/gcc) for the verified-root host library')
    command = [compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC', '-I', ctx.root / 'kits/VerifiedRoot/include']
    prefix = os.environ.get('UNIXNU_OPENSSL_PREFIX')
    if prefix:
        command += ['-I', f'{prefix}/include', '-L', f'{prefix}/lib']
    command += [*sources, '-lcrypto', '-o', library]
    log('policies: building the host verifier ' + library.name)
    run(command, capture=True)
    return library


def _apfs_regions(stream, size):
    """Where APFS containers can start in the window: each GPT partition when the window is a disk (the OS image
    is one, its partition starts at LBA 34, 1024 bytes off a 4 KiB boundary), else the window itself."""
    stream.seek(0)
    head = stream.read(1024)
    if len(head) >= 1024 and head[512:520] == b'EFI PART':
        entries_lba, count, entry_bytes = struct.unpack_from('<Q', head, 584)[0], *struct.unpack_from('<II', head, 592)
        stream.seek(entries_lba * 512)
        table = stream.read(count * entry_bytes)
        regions = []
        for index in range(count):
            entry = table[index * entry_bytes:(index + 1) * entry_bytes]
            if len(entry) < 56 or not any(entry[:16]):
                continue
            first, last = struct.unpack_from('<QQ', entry, 32)
            start, length = first * 512, (last - first + 1) * 512
            if start + length <= size:
                regions.append((start, length))
        return regions
    return [(0, size)]


def apfs_volume_uuids(stream, size, progress=None):
    """The volume UUIDs of every APFS volume superblock (APSB) at a 4 KiB block boundary of a container in the
    window, with their counts (every checkpoint copy of a volume superblock carries the same UUID)."""
    seen = {}
    for start, length in _apfs_regions(stream, size):
        stream.seek(start)
        probe = stream.read(BLOCK)
        if probe[32:36] != b'NXSB':
            continue
        position = 0
        stream.seek(start)
        while position < length:
            buf = stream.read(min(64 << 20, length - position))
            if not buf:
                break
            for off in range(0, len(buf) - 255, BLOCK):
                if buf[off + 32:off + 36] == b'APSB':
                    found = uuid.UUID(bytes=buf[off + 240:off + 256])
                    seen[found] = seen.get(found, 0) + 1
            position += len(buf)
            if progress:
                progress(start + position)
    return seen


def _verify(library, policy_dir, stream, size, expected_uuid, sample_indices):
    """Every block through the C provider, as the macOS policy builder does; returns (sha256, samples, lengths)."""
    import ctypes

    class View(ctypes.Structure):
        _fields_ = [('hashes', ctypes.c_void_p), ('image_bytes', ctypes.c_uint64), ('block_count', ctypes.c_uint64),
                    ('generation', ctypes.c_uint64), ('volume_uuid', ctypes.c_uint8 * 16), ('image_sha256', ctypes.c_uint8 * 32)]

    native = ctypes.CDLL(str(library))
    native.vr_host_open.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(View)]
    native.vr_host_open.restype = ctypes.c_int
    native.vr_host_block.argtypes = [ctypes.POINTER(View), ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
    native.vr_host_block.restype = ctypes.c_int
    encoded = ctypes.create_string_buffer((policy_dir / 'manifest.bin').read_bytes())
    anchor = ctypes.create_string_buffer((policy_dir / 'trusted-key.bin').read_bytes())
    view = View()
    if native.vr_host_open(encoded, len(encoded) - 1, anchor, 1, ctypes.byref(view)):
        raise Error(f'{policy_dir.name}: the C provider refused the signed manifest')
    if bytes(view.volume_uuid) != expected_uuid.bytes or view.image_bytes != size:
        raise Error(f'{policy_dir.name}: the signed identity does not match the window')
    complete = hashlib.sha256()
    samples, lengths = [], []
    stream.seek(0)
    for index in range(view.block_count):
        block = stream.read(min(BLOCK, size - index * BLOCK))
        if native.vr_host_block(ctypes.byref(view), index, block, len(block)) != 0:
            raise Error(f'{policy_dir.name}: block {index} failed verification against its own policy')
        complete.update(block)
        if index in sample_indices:
            samples.append(block.ljust(BLOCK, b'\0'))
            lengths.append(len(block))
    if complete.digest() != bytes(view.image_sha256):
        raise Error(f'{policy_dir.name}: the complete digest differs from the signed one')
    return complete.hexdigest(), b''.join(samples), lengths


def sign_window(ctx, manifest, library, keys, name, offset, size, window_sha256, expected_uuid, generation=1):
    policy_dir = ctx.tree / f'build/verified-root-{POLICY_DIRS[name]}'
    private, public = keys
    anchor_sha256 = hashlib.sha256(manifest.public_anchor(public)).hexdigest()
    report = policy_dir / 'verification.json'
    if report.is_file():
        previous = json.loads(report.read_text())
        if (previous.get('image_sha256') == window_sha256 and previous.get('trusted_key_sha256') == anchor_sha256 and
                previous.get('volume_uuid') == str(expected_uuid).upper() and previous.get('generation') == generation and
                (policy_dir / 'manifest.bin').is_file() and (policy_dir / 'original-samples.bin').is_file()):
            log(f'policies: {name}: policy up to date ({policy_dir})')
            return policy_dir
    _, image, _ = userdata.paths(ctx)
    stream = _Window(image, offset, size)
    try:
        log(f'policies: {name}: looking for the volume {str(expected_uuid).upper()} in the window')
        volumes = apfs_volume_uuids(stream, size)
        if expected_uuid not in volumes:
            raise Error(f'{name}: volume {str(expected_uuid).upper()} is not in the window; found '
                        + ', '.join(str(v).upper() for v in volumes))
        log(f'policies: {name}: signing {size} bytes ({(size + BLOCK - 1) // BLOCK} blocks) as generation {generation}')
        stream.seek(0)
        record = manifest.make(stream, size, private, public, expected_uuid, generation, policy_dir)
        if record['image_sha256'] != window_sha256:
            raise Error(f'{name}: the window digest {record["image_sha256"]} differs from userdata-root.json ({window_sha256})')
        count = record['block_count']
        indices = [0, 1, count // 2, count - 1]
        log(f'policies: {name}: verifying every block with the C provider')
        digest, samples, lengths = _verify(library, policy_dir, stream, size, expected_uuid, set(indices))
    finally:
        stream.close()
    (policy_dir / 'original-samples.bin').write_bytes(samples)
    data = dict(image_bytes=size, blocks_verified=count, image_sha256=digest, volume_uuid=str(expected_uuid).upper(),
                generation=generation, source='userdata-root.img window ' + name, source_offset=offset,
                trusted_key_sha256=record['trusted_key_sha256'], actual_sample_indices=indices,
                actual_sample_lengths=lengths, real_c_signature_and_block_verification=True,
                original_image_modified=False, host_only=True, boot_root_authenticated=False,
                qemu_tested=False, hardware_tested=False)
    report.write_text(json.dumps(data, indent=2) + '\n')
    log(f'policies: {name}: PASS, {count} blocks signed and verified, policy in {policy_dir}')
    return policy_dir


def build(ctx):
    log('policies: owner-verified root policies')
    _, image, report = userdata.paths(ctx)
    if not image.is_file() or not report.is_file():
        raise Error('no root image: run "unixnu build userdata" first')
    data = json.loads(report.read_text())
    if hashlib.sha256(image.open('rb').read(BLOCK)).hexdigest() != data.get('manifest_sha256'):
        raise Error(f'{image} does not match {report}; rebuild the root image')
    adt = ctx.board.get('payload', {}).get('adt', {})
    root_uuid = adt.get('root_match_volume_uuid')
    if not root_uuid:
        raise Error('board.toml needs [payload.adt] root_match_volume_uuid (the OS volume the kernel roots on)')
    ctx.tree.mkdir(parents=True, exist_ok=True)
    (ctx.tree / 'build').mkdir(exist_ok=True)
    host = ctx.tree / 'build/verified-root-host'
    host.mkdir(exist_ok=True)
    keys = ensure_keys(host / 'keys')
    library = host_library(ctx, host)
    manifest = _manifest_module(ctx)
    results = {}
    for window in data['windows']:
        name = window['name']
        if name not in POLICY_DIRS:
            raise Error(f'window {name}: no policy directory is defined for it (known: {", ".join(POLICY_DIRS)})')
        if name == 'OS':
            expected = uuid.UUID(root_uuid)
        else:
            stream = _Window(image, window['offset'], window['size'])
            try:
                volumes = apfs_volume_uuids(stream, window['size'])
            finally:
                stream.close()
            if len(volumes) != 1:
                raise Error(f'{name}: expected one APFS volume in the window, found ' + ', '.join(str(v).upper() for v in volumes))
            expected = next(iter(volumes))
        results[name] = sign_window(ctx, manifest, library, keys, name, window['offset'], window['size'], window['sha256'], expected)
    summary = {name: dict(policy=str(path), **{k: v for k, v in json.loads((path / 'verification.json').read_text()).items()
                                               if k in ('image_bytes', 'blocks_verified', 'image_sha256', 'volume_uuid', 'trusted_key_sha256')})
               for name, path in results.items()}
    (ctx.work / 'userdata' / 'policies.json').write_text(json.dumps(summary, indent=2) + '\n')
    log('policies: done; the kernel step builds the probe archive from them (build-verified-root-probe.py)')
