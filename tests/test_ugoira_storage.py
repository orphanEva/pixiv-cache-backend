from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.models.work import WorkType
from app.schemas.pixiv import RemoteSnapshot, UgoiraFrame
from app.storage.local_storage import LocalStorage


def make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("000000.jpg", b"frame-zero")
        archive.writestr("000001.jpg", b"frame-one")


def test_extract_ugoira_frames_validates_expected_members(tmp_path: Path):
    settings = get_settings()
    old_root = settings.storage_root
    settings.storage_root = tmp_path
    try:
        storage = LocalStorage()
        zip_path = tmp_path / "source.zip"
        make_zip(zip_path)
        frames = [UgoiraFrame(file="000000.jpg", delay=100), UgoiraFrame(file="000001.jpg", delay=200)]
        paths = storage._extract_ugoira_frames(zip_path, frames, tmp_path / "frames")
        assert [p.read_bytes() for p in paths] == [b"frame-zero", b"frame-one"]
    finally:
        settings.storage_root = old_root


@pytest.mark.asyncio
async def test_materialize_ugoira_keeps_raw_meta_and_derived_mp4(tmp_path: Path, monkeypatch):
    settings = get_settings()
    old_root = settings.storage_root
    old_mp4 = settings.ugoira_generate_mp4
    old_keep = settings.ugoira_keep_extracted_frames
    settings.storage_root = tmp_path
    settings.ugoira_generate_mp4 = True
    settings.ugoira_keep_extracted_frames = False
    storage = LocalStorage()
    storage.root = tmp_path

    fixture_zip = tmp_path / "fixture.zip"
    make_zip(fixture_zip)

    async def fake_download(client, url, path, **kwargs):
        data = fixture_zip.read_bytes()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)

    async def fake_convert(frame_paths, frames, target, output):
        assert [f.delay for f in frames] == [100, 200]
        output.write_bytes(b"fake-mp4")

    monkeypatch.setattr(storage, "_download_stream", fake_download)
    monkeypatch.setattr(storage, "_convert_ugoira_to_mp4", fake_convert)

    snapshot = RemoteSnapshot(
        pixiv_id=77,
        work_type=WorkType.UGOIRA,
        title="ugoira",
        version_token="a" * 64,
        ugoira_zip_url="https://i.pximg.net/source.zip",
        ugoira_frames=[
            UgoiraFrame(file="000000.jpg", delay=100),
            UgoiraFrame(file="000001.jpg", delay=200),
        ],
    )
    try:
        root, assets = await storage.materialize(snapshot, 1)
        assert Path(root).exists()
        assert {a["file_type"] for a in assets} == {"ugoira_zip", "ugoira_meta", "ugoira_mp4"}
        assert (Path(root) / "original.zip").is_file()
        assert (Path(root) / "ugoira_meta.json").is_file()
        assert (Path(root) / "preview.mp4").read_bytes() == b"fake-mp4"
        assert not (Path(root) / "frames").exists()
    finally:
        settings.storage_root = old_root
        settings.ugoira_generate_mp4 = old_mp4
        settings.ugoira_keep_extracted_frames = old_keep
