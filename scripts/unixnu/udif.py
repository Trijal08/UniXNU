"""Apple UDIF disk images (.dmg) without hdiutil: the raw disk bytes behind the block map.

A UDIF file ends in a 512-byte 'koly' trailer that points at an XML property list; its resource fork
holds one 'blkx' table per partition, each a list of chunks (type, output sector, sector count, offset
and length in the data fork). Chunk types: zero fill, raw copy, ignore (zero), zlib, bzip2, lzma and
lzfse (the Apple restore images use lzfse and raw). The image is expanded strictly in output order, so
the reader only needs a seekable source and writes a stream.

lzfse comes from liblzfse (the project's own build under the Linux toolchain or a system library);
set UNIXNU_LZFSE to the shared library when it is not found automatically.
"""
import bz2
import ctypes
import ctypes.util
import lzma
import os
from pathlib import Path
import plistlib
import struct
import zlib

SECTOR = 512
CHUNK_ZERO, CHUNK_RAW, CHUNK_IGNORE = 0x00000000, 0x00000001, 0x00000002
CHUNK_ADC, CHUNK_ZLIB, CHUNK_BZIP2, CHUNK_LZFSE, CHUNK_LZMA = 0x80000004, 0x80000005, 0x80000006, 0x80000007, 0x80000008
CHUNK_COMMENT, CHUNK_END = 0x7ffffffe, 0xffffffff
_ZEROS = bytes(1 << 20)


class UdifError(Exception):
    pass


_lzfse = None


def _lzfse_library():
    global _lzfse
    if _lzfse is not None:
        return _lzfse
    candidates = [os.environ.get('UNIXNU_LZFSE')]
    here = Path(__file__).resolve()
    candidates += [str(p) for p in (Path.home() / 'Builds/unixnu-linux/lib/liblzfse.so',)]
    candidates.append(ctypes.util.find_library('lzfse'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            lib = ctypes.CDLL(candidate)
            lib.lzfse_decode_scratch_size.restype = ctypes.c_size_t
            lib.lzfse_decode_buffer.restype = ctypes.c_size_t
            lib.lzfse_decode_buffer.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
            _lzfse = (lib, ctypes.create_string_buffer(lib.lzfse_decode_scratch_size()))
            return _lzfse
    raise UdifError('liblzfse not found: build it or set UNIXNU_LZFSE to the shared library')


def _lzfse_decode(data, expected):
    lib, scratch = _lzfse_library()
    out = ctypes.create_string_buffer(expected)
    got = lib.lzfse_decode_buffer(out, expected, data, len(data), scratch)
    if got != expected:
        raise UdifError(f'lzfse chunk decoded to {got} bytes, expected {expected}')
    return out.raw


class RawImage:
    """A plain disk image (no UDIF trailer), such as the SystemOS cryptex member: `size` and `expand` match Udif."""

    def __init__(self, stream):
        self.stream = stream
        stream.seek(0, 2)
        self.size = stream.tell()
        if self.size == 0 or self.size % SECTOR:
            raise UdifError(f'raw image size {self.size} is not a whole number of {SECTOR}-byte sectors')
        self.chunks = [(0, self.size // SECTOR, CHUNK_RAW, 0, self.size)]

    def expand(self, write, progress=None):
        self.stream.seek(0)
        position = 0
        while position < self.size:
            data = self.stream.read(min(1 << 24, self.size - position))
            if not data:
                raise UdifError(f'short read of the raw image at {position}')
            write(data)
            position += len(data)
            if progress:
                progress(1 if position == self.size else 0, 1, position)
        return position


def open_image(stream):
    """Udif when the stream ends in a koly trailer, else RawImage; raw APFS containers have NXSB at offset 32."""
    stream.seek(0, 2)
    end = stream.tell()
    if end >= 512:
        stream.seek(end - 512)
        if stream.read(4) == b'koly':
            return Udif(stream)
    stream.seek(32)
    if stream.read(4) != b'NXSB':
        raise UdifError('neither a UDIF (no koly trailer) nor a raw APFS container (no NXSB at offset 32)')
    return RawImage(stream)


class Udif:
    """A parsed UDIF: `size` is the expanded disk size in bytes; `expand(write)` streams the disk."""

    def __init__(self, stream):
        self.stream = stream
        stream.seek(0, 2)
        end = stream.tell()
        if end < 512:
            raise UdifError('file too small for a UDIF trailer')
        stream.seek(end - 512)
        koly = stream.read(512)
        if koly[:4] != b'koly':
            raise UdifError('no UDIF trailer (koly) at the end of the file')
        (self.data_offset, self.data_length) = struct.unpack_from('>QQ', koly, 24)
        (xml_offset, xml_length) = struct.unpack_from('>QQ', koly, 216)
        (self.sector_count,) = struct.unpack_from('>Q', koly, 492)
        self.size = self.sector_count * SECTOR
        stream.seek(xml_offset)
        plist = plistlib.loads(stream.read(xml_length))
        self.chunks = []
        for entry in plist['resource-fork']['blkx']:
            table = entry['Data']
            if table[:4] != b'mish':
                raise UdifError('blkx entry without a mish table')
            (first_sector,) = struct.unpack_from('>Q', table, 8)
            (count,) = struct.unpack_from('>I', table, 200)
            for index in range(count):
                kind, _, sector, sectors, offset, length = struct.unpack_from('>IIQQQQ', table, 204 + 40 * index)
                if kind in (CHUNK_END, CHUNK_COMMENT):
                    continue
                self.chunks.append((first_sector + sector, sectors, kind, offset, length))
        self.chunks.sort()
        position = 0
        for sector, sectors, kind, offset, length in self.chunks:
            if sector < position:
                raise UdifError(f'overlapping chunks at sector {sector}')
            position = sector + sectors
        if position > self.sector_count:
            raise UdifError('block map extends past the image size')

    def _decode(self, kind, data, expected):
        if kind == CHUNK_RAW:
            if len(data) != expected:
                raise UdifError('raw chunk length mismatch')
            return data
        if kind == CHUNK_ZLIB:
            out = zlib.decompress(data)
        elif kind == CHUNK_BZIP2:
            out = bz2.decompress(data)
        elif kind == CHUNK_LZMA:
            out = lzma.decompress(data)
        elif kind == CHUNK_LZFSE:
            return _lzfse_decode(data, expected)
        elif kind == CHUNK_ADC:
            raise UdifError('ADC compressed chunks are not supported')
        else:
            raise UdifError(f'unknown chunk type {kind:#x}')
        if len(out) != expected:
            raise UdifError(f'chunk decoded to {len(out)} bytes, expected {expected}')
        return out

    def expand(self, write, progress=None):
        """Call write(bytes) for the whole disk, in order, zero-filling the gaps. Returns the byte count."""
        position = 0
        done = 0
        for sector, sectors, kind, offset, length in self.chunks:
            start = sector * SECTOR
            while position < start:
                step = min(len(_ZEROS), start - position)
                write(_ZEROS[:step])
                position += step
            expected = sectors * SECTOR
            if kind in (CHUNK_ZERO, CHUNK_IGNORE):
                remaining = expected
                while remaining:
                    step = min(len(_ZEROS), remaining)
                    write(_ZEROS[:step])
                    remaining -= step
            else:
                self.stream.seek(self.data_offset + offset)
                data = self.stream.read(length)
                if len(data) != length:
                    raise UdifError(f'short read of chunk at {offset}')
                write(self._decode(kind, data, expected))
            position += expected
            done += 1
            if progress:
                progress(done, len(self.chunks), position)
        while position < self.size:
            step = min(len(_ZEROS), self.size - position)
            write(_ZEROS[:step])
            position += step
        return position
