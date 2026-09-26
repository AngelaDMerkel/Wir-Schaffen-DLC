"""Fixed early-load integration for the verified Aspyr x86-64 executable.

The product archive supplies a signed correction library. WSDLC adds only its
fixed load command in existing header padding; product data cannot select patch
addresses, executable bytes, library paths, or signing commands.
"""
from pathlib import Path
import hashlib
import shutil
import struct
import subprocess

HOST_SHA256 = 'd56d6bfbc0ef517fcb7cbaff46c42d1bdfab809c084684045761bd9d85807ee9'
HOST_RELATIVE = 'Contents/MacOS/Civilization V'
LIBRARY = 'libWirCiv5HostStat.dylib'
LIBRARY_RELATIVE = 'Contents/MacOS/' + LIBRARY
LOAD_PATH = '@executable_path/' + LIBRARY
KIND = 'sqlite-stat-inode64-v1'


class HostStartupError(ValueError):
    pass


def add_load_command(data: bytes) -> bytes:
    """Append a fixed dependency without relocating any existing section."""
    if len(data) < 32:
        raise HostStartupError('truncated Mach-O header')
    magic, cpu, _, kind, count, size, _, _ = struct.unpack_from('<8I', data)
    end = 32 + size
    if (magic, cpu, kind) != (0xfeedfacf, 0x1000007, 2) or count > 128 or end > len(data):
        raise HostStartupError('unsupported executable header')
    offset, sections = 32, []
    for _ in range(count):
        if offset + 8 > end:
            raise HostStartupError('truncated load command')
        command, length = struct.unpack_from('<II', data, offset)
        if length < 8 or length % 8 or offset + length > end:
            raise HostStartupError('invalid load command size')
        if command == 0x19:
            if length < 72:
                raise HostStartupError('truncated segment')
            nsects = struct.unpack_from('<I', data, offset + 64)[0]
            if 72 + nsects * 80 != length:
                raise HostStartupError('invalid section table')
            for index in range(nsects):
                section = offset + 72 + index * 80
                file_offset = struct.unpack_from('<I', data, section + 48)[0]
                if file_offset:
                    sections.append(file_offset)
        if command in (0xc, 0x80000018, 0x8000001f):
            if length < 24:
                raise HostStartupError('truncated library command')
            name_offset = struct.unpack_from('<I', data, offset + 8)[0]
            if name_offset < 24 or name_offset >= length:
                raise HostStartupError('invalid library name')
            if data[offset + name_offset:offset + length].split(b'\0', 1)[0] == LOAD_PATH.encode():
                raise HostStartupError('startup dependency already exists')
        offset += length
    if offset != end or not sections:
        raise HostStartupError('invalid executable layout')
    name = LOAD_PATH.encode() + b'\0'
    length = (24 + len(name) + 7) & ~7
    if end + length > min(sections) or any(data[end:end + length]):
        raise HostStartupError('insufficient unused load-command padding')
    result = bytearray(data)
    struct.pack_into('<II', result, 16, count + 1, size + length)
    command = struct.pack('<6I', 0xc, length, 24, 0, 0, 0) + name
    result[end:end + length] = command.ljust(length, b'\0')
    return bytes(result)


def prepare_host(original: Path, destination: Path) -> None:
    """Prepare and sign a copy; never modify the live executable or backup."""
    data = original.read_bytes()
    if hashlib.sha256(data).hexdigest() != HOST_SHA256:
        raise HostStartupError('startup correction does not support this executable hash')
    patched = add_load_command(data)
    shutil.copy2(original, destination)
    destination.write_bytes(patched)
    try:
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none',
                        '--identifier', 'Civilization V', str(destination)],
                       check=True, capture_output=True, text=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(destination)],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise HostStartupError('startup executable signature failed: ' + error.stderr.strip()) from error
