"""Read the file list of a remote ZIP from its central directory in one or two ranged reads."""

import struct
from collections.abc import Callable

# the end record is at most this far from the end, behind a comment of up to 65,535 bytes
END_SEARCH = 22 + 65535
# the central directory of most NOMAD uploads fits in the first read
TAIL = 2 << 20

# reads a byte range such as "bytes=-100" or "bytes=0-99"; returns the bytes and the ZIP's size
Fetch = Callable[[str], tuple[bytes, int]]


class ZipIndexError(Exception):
    """The ZIP could not be read."""


def list_files(fetch: Fetch, max_directory: int) -> dict[str, int | None]:
    """Map each path in the ZIP to its size, or to None for a folder without files.

    A central directory larger than ``max_directory`` bytes is refused rather than read.
    """
    tail, total = fetch(f"bytes=-{TAIL}")
    base = total - len(tail)
    end = tail.rfind(b"PK\x05\x06", max(0, len(tail) - END_SEARCH))
    if end == -1 or end + 22 > len(tail):
        raise ZipIndexError("no end of central directory")
    count, size, offset = struct.unpack_from("<HII", tail, end + 10)
    locator = end - 20
    if locator >= 0 and tail[locator : locator + 4] == b"PK\x06\x07":
        (record,) = struct.unpack_from("<Q", tail, locator + 8)
        header = (
            tail[record - base : record - base + 56]
            if record >= base
            else fetch(f"bytes={record}-{record + 55}")[0]
        )
        if header[:4] != b"PK\x06\x06":
            raise ZipIndexError("no ZIP64 end of central directory")
        count, size, offset = struct.unpack_from("<QQQ", header, 32)
    if size > max_directory:
        raise ZipIndexError(f"central directory of {size} bytes is too large")
    if offset >= base:
        directory = tail[offset - base : offset - base + size]
    else:
        directory = fetch(f"bytes={offset}-{offset + size - 1}")[0]
    files = _parse(directory)
    if len(files) > count:
        raise ZipIndexError("more entries than the end record counts")
    return files


def _parse(directory: bytes) -> dict[str, int | None]:
    files: dict[str, int | None] = {}
    pos = 0
    while pos + 46 <= len(directory):
        if directory[pos : pos + 4] != b"PK\x01\x02":
            raise ZipIndexError("broken central directory")
        (flags,) = struct.unpack_from("<H", directory, pos + 8)
        size, name_length, extra_length, comment_length = struct.unpack_from(
            "<IHHH", directory, pos + 24
        )
        start = pos + 46
        raw = directory[start : start + name_length]
        name = raw.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        if size == 0xFFFFFFFF:
            size = _zip64_size(directory[start + name_length : start + name_length + extra_length])
        parts = [part for part in name.split("/") if part]
        # a ZIP may name a path outside itself; such entries are left out
        if parts and not any(part in (".", "..") for part in parts):
            files["/".join(parts)] = None if name.endswith("/") else size
        pos = start + name_length + extra_length + comment_length
    return files


def _zip64_size(extra: bytes) -> int:
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, pos)
        if tag == 0x0001 and length >= 8:
            return int(struct.unpack_from("<Q", extra, pos + 4)[0])
        pos += 4 + length
    raise ZipIndexError("ZIP64 size missing")
