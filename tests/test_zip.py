import io
import re
import struct
import zipfile

import pytest
import requests
import responses

from nomad_fsspec import NomadFileSystem, filesystem, zipindex

BASE = "https://nomad.example.org/oasis"
API = f"{BASE}/api/v1"
ZIP_URL = f"{API}/uploads/up1/raw"


def make_zip(files: dict[str, bytes], folders: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for folder in folders:
            archive.writestr(folder, b"")
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def large_upload(upload: str, **extra) -> None:
    data = {"published": True, "entries": filesystem.ZIP_MIN_ENTRIES + 1, **extra}
    responses.get(f"{API}/uploads/{upload}", json={"data": data})


def serve_zip(data: bytes, seen: list[dict]) -> None:
    large_upload("up1")

    def ranged(request):
        seen.append(dict(request.headers))
        start, end = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers["Range"]).groups()
        if not start:
            start, end = max(0, len(data) - int(end)), len(data) - 1
        start, end = int(start), min(int(end), len(data) - 1)
        headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
        return 206, headers, data[start : end + 1]

    responses.add_callback(responses.GET, ZIP_URL, callback=ranged)


@responses.activate
def test_a_published_upload_is_listed_from_its_zip_and_kept():
    seen: list[dict] = []
    serve_zip(
        make_zip({"a.txt": b"abc", "sub/b.txt": b"bb", "sub/deep/c.txt": b"c"}, ("empty/",)), seen
    )
    fs = NomadFileSystem(base_url=BASE, skip_instance_cache=True)
    assert [(e["name"], e["type"], e["size"]) for e in fs.ls("/datasets/ds1/up1")] == [
        ("/datasets/ds1/up1/a.txt", "file", 3),
        ("/datasets/ds1/up1/empty", "directory", 0),
        ("/datasets/ds1/up1/sub", "directory", 0),
    ]
    assert fs.ls("/datasets/ds1/up1/sub/deep", detail=False) == ["/datasets/ds1/up1/sub/deep/c.txt"]
    assert fs.ls("/datasets/ds1/up1/sub/b.txt") == [
        {"name": "/datasets/ds1/up1/sub/b.txt", "size": 2, "type": "file"}
    ]
    with pytest.raises(FileNotFoundError):
        fs.ls("/datasets/ds1/up1/gone")
    # one ranged read, without the token, shared by a later instance that has one
    other = NomadFileSystem(base_url=BASE, token="secret", skip_instance_cache=True)
    assert other.ls("/uploads/up1/sub", detail=False) == [
        "/uploads/up1/sub/b.txt",
        "/uploads/up1/sub/deep",
    ]
    assert len(seen) == 1 and "Authorization" not in seen[0]


@responses.activate
def test_a_large_zip64_directory_takes_a_second_read(monkeypatch):
    monkeypatch.setattr(zipindex, "TAIL", 4096)
    seen: list[dict] = []
    serve_zip(make_zip({f"d{i % 100}/f{i}.txt": b"x" for i in range(65536)}), seen)
    fs = NomadFileSystem(base_url=BASE, skip_instance_cache=True)
    assert len(fs.ls("/datasets/ds1/up1")) == 100
    assert fs.info("/datasets/ds1/up1/d7/f65507.txt")["size"] == 1
    assert len(seen) == 2


@responses.activate
def test_small_unpublished_or_unreadable_uploads_are_listed_folder_by_folder():
    large_upload("up1", published=False)
    large_upload("up2")
    responses.get(f"{API}/uploads/up2/raw", status=401, json={"detail": "embargoed"})
    large_upload("up3")
    responses.get(
        f"{API}/uploads/up3/raw", status=429, headers={"Retry-After": "0"}, json={"detail": "busy"}
    )
    responses.get(f"{API}/uploads/up4", json={"data": {"published": True, "entries": 3}})
    for upload in ("up1", "up2", "up3", "up4"):
        responses.get(
            f"{API}/uploads/{upload}/rawdir/",
            json={
                "directory_metadata": {"content": [{"name": "a", "is_file": True, "size": 1}]},
                "pagination": {"total": 1},
            },
        )
    fs = NomadFileSystem(base_url=BASE, skip_instance_cache=True)
    assert fs.ls("/uploads/up1", detail=False) == ["/uploads/up1/a"]
    assert fs.ls("/datasets/ds1/up4", detail=False) == ["/datasets/ds1/up4/a"]

    def zip_reads(upload):
        return sum(call.request.url == f"{API}/uploads/{upload}/raw" for call in responses.calls)

    assert zip_reads("up1") == zip_reads("up4") == 0
    for upload in ("up2", "up3"):
        assert fs.ls(f"/datasets/ds1/{upload}", detail=False) == [f"/datasets/ds1/{upload}/a"]
    before = zip_reads("up2"), zip_reads("up3")
    for upload in ("up2", "up3"):
        fs.ls(f"/datasets/ds1/{upload}", refresh=True)
    # a missing ZIP is not asked for again, but a busy NOMAD is
    assert zip_reads("up2") == before[0] == 1
    assert zip_reads("up3") > before[1]


@responses.activate
def test_a_small_upload_is_read_from_its_zip_when_nomad_is_slow_to_list_it():
    seen: list[dict] = []
    serve_zip(make_zip({"sub/a.txt": b"abc", "sub/deeper/b.txt": b"b"}), seen)
    responses.replace(
        responses.GET, f"{API}/uploads/up1", json={"data": {"published": True, "entries": 3}}
    )
    responses.get(f"{API}/uploads/up1/rawdir/sub/", body=requests.exceptions.ReadTimeout())
    fs = NomadFileSystem(base_url=BASE, skip_instance_cache=True)
    assert fs.ls("/datasets/ds1/up1/sub", detail=False) == [
        "/datasets/ds1/up1/sub/a.txt",
        "/datasets/ds1/up1/sub/deeper",
    ]
    assert fs.ls("/datasets/ds1/up1/sub/deeper", detail=False) == [
        "/datasets/ds1/up1/sub/deeper/b.txt"
    ]
    folder_listings = [call for call in responses.calls if "/rawdir/" in call.request.url]
    assert len(folder_listings) == 1 and len(seen) == 1


@responses.activate
def test_a_server_that_ignores_the_range_is_not_read():
    large_upload("up1")
    responses.get(ZIP_URL, body=make_zip({"a.txt": b"abc"}))
    responses.get(
        f"{API}/uploads/up1/rawdir/",
        json={"directory_metadata": {"content": []}, "pagination": {"total": 0}},
    )
    fs = NomadFileSystem(base_url=BASE, skip_instance_cache=True)
    assert fs.ls("/datasets/ds1/up1") == []


def directory_record(name: bytes, size: int, flags: int = 0, extra: bytes = b"") -> bytes:
    lengths = (len(name), len(extra), 0, 0, 0)
    return (
        struct.pack(
            "<4s6H3I5H2I", b"PK\x01\x02", 20, 20, flags, 0, 0, 0, 0, 0, size, *lengths, 0, 0
        )
        + name
        + extra
    )


def test_names_paths_and_zip64_sizes_are_read_from_the_directory():
    directory = b"".join(
        [
            directory_record("é.txt".encode(), 1, flags=0x800),
            directory_record(b"\x82t.txt", 2),  # cp437 without the UTF-8 flag
            directory_record(b"big.bin", 0xFFFFFFFF, extra=struct.pack("<HHQ", 0x0001, 8, 5 << 32)),
            directory_record(b"../outside.txt", 3),
            directory_record(b"folder/", 0),
            directory_record(b"a/b/c.txt", 4),
        ]
    )
    tree = zipindex._parse(directory, 6)
    assert zipindex.children(tree, "") == [
        ("a", None),
        ("big.bin", 5 << 32),
        ("folder", None),
        ("é.txt", 1),
        ("ét.txt", 2),
    ]
    assert zipindex.children(tree, "a") == [("b", None)]
    assert zipindex.children(tree, "a/b") == [("c.txt", 4)]
    assert zipindex.children(tree, "folder") == []
    assert zipindex.children(tree, "gone") is None
