"""Offline container smoke test: real FFmpeg, synthetic frames, no Pixiv access."""
import asyncio
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from app.schemas.pixiv import UgoiraFrame
from app.storage.local_storage import LocalStorage


def png(rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + bytes(rgb) * 2) * 2))
            + chunk(b'IEND', b''))


async def main():
    with tempfile.TemporaryDirectory(prefix='pixiv-conversion-smoke-') as folder:
        root = Path(folder)
        frames = [UgoiraFrame(file='a.png', delay=100), UgoiraFrame(file='b.png', delay=200)]
        paths = [root / frame.file for frame in frames]
        for path, rgb in zip(paths, [(255, 0, 0), (0, 0, 255)]):
            path.write_bytes(png(rgb))
        output = root / 'preview.mp4'
        await LocalStorage()._convert_ugoira_to_mp4(paths, frames, root, output)
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output),
        ], check=True, capture_output=True, timeout=30)
        report = json.loads(probe.stdout)
        assert any(stream['codec_type'] == 'video' for stream in report['streams'])
        assert not any(stream['codec_type'] == 'audio' for stream in report['streams'])
        assert 0.25 <= float(report['format']['duration']) <= 0.6
        assert output.stat().st_size > 0
        print('Offline Ugoira -> MP4 smoke test passed')


if __name__ == '__main__':
    asyncio.run(main())
