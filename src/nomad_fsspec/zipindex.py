"""Read the file list of a remote ZIP from its central directory in one or two ranged reads."""

import struct
from array import array
from collections.abc import Callable

# the end record is at most this far from the end, behind a comment of up to 65,535 bytes
END_SEARCH = 22 + 65535
# the central directory of most NOMAD uploads fits in the first read
TAIL = 2 << 20

# reads a byte range such as "bytes=-100" or "bytes=0-99"; returns the bytes and the ZIP's size
Fetch = Callable[[str], tuple[bytes, int]]
# each folder ("" is the top) holds its children's names, NUL-separated UTF-8, and their sizes,
# -1 for a folder: one bytes object and one array per folder instead of objects per file
Tree = dict[str, tuple[bytes, array]]


class ZipIndexError(Exception):
    """The ZIP could not be read."""


def read_tree(fetch: Fetch, max_directory: int) -> Tree:
    """Read the folders and files of a ZIP.

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
    return _parse(directory, count)


def children(tree: Tree, folder: str) -> list[tuple[str, int | None]] | None:
    """The children of a folder by name, with their sizes or None for folders; None if missing."""
    kept = tree.get(folder)
    if kept is None:
        return None
    raw, sizes = kept
    named = dict(zip(raw.decode().split("\0")[:-1], sizes, strict=True))
    return sorted((name, None if size < 0 else size) for name, size in named.items())


def size_of(tree: Tree) -> int:
    """Roughly the bytes a tree takes in memory."""
    return sum(len(raw) + sizes.itemsize * len(sizes) + 200 for raw, sizes in tree.values())


def _parse(directory: bytes, count: int) -> Tree:
    names: dict[str, bytearray] = {"": bytearray()}
    sizes: dict[str, array] = {"": array("q")}

    def add(folder: str, name: str, size: int) -> None:
        names[folder] += name.encode() + b"\0"
        sizes[folder].append(size)

    def link(folder: str) -> None:
        # adds a folder and any missing parents, each listed once in its parent
        if folder in names:
            return
        parent, _, name = folder.rpartition("/")
        link(parent)
        names[folder], sizes[folder] = bytearray(), array("q")
        add(parent, name, -1)

    pos = records = 0
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
            if name.endswith("/"):
                link("/".join(parts))
            else:
                folder = "/".join(parts[:-1])
                link(folder)
                add(folder, parts[-1], size)
        records += 1
        pos = start + name_length + extra_length + comment_length
    if records > count:
        raise ZipIndexError("more entries than the end record counts")
    return {folder: (bytes(names[folder]), sizes[folder]) for folder in names}


def _zip64_size(extra: bytes) -> int:
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, pos)
        if tag == 0x0001 and length >= 8:
            return int(struct.unpack_from("<Q", extra, pos + 4)[0])
        pos += 4 + length
    raise ZipIndexError("ZIP64 size missing")
