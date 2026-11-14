"""测试夹具：内存版 S3（模拟 MinIO multipart 语义）+ 临时 SQLite。"""
from __future__ import annotations

import io
import threading
import uuid
from pathlib import Path

import pytest

from app import db as db_mod
from app.config import PART_SIZE
from app.service import UploadService
from app.storage import sha256_hex


class FakeS3Error(Exception):
    def __init__(self, code: str):
        self.code = code


class _FakeStream:
    """对齐 boto3 StreamingBody 的最小接口（iter_chunks/close/read）。"""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def iter_chunks(self, chunk_size: int):
        while True:
            chunk = self._buf.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def read(self, *args):
        return self._buf.read(*args)

    def close(self):
        self._buf.close()


class FakeS3:
    """单进程内存实现，行为对齐 boto3 S3 multipart 关键语义。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.buckets: set[str] = set()
        # upload_id -> dict(key, parts: {part_number: (body, etag)})
        self.uploads: dict[str, dict] = {}
        # object_key -> bytes
        self.objects: dict[str, bytes] = {}

    # ---- bucket ----
    def bucket_exists(self) -> bool:
        return "test-bucket" in self.buckets

    def ensure_bucket(self) -> None:
        self.buckets.add("test-bucket")

    # ---- multipart ----
    def create_multipart_upload(self, object_key: str) -> str:
        with self._lock:
            upload_id = "mp-" + uuid.uuid4().hex
            self.uploads[upload_id] = {"key": object_key, "parts": {}}
            return upload_id

    def upload_part(self, object_key: str, upload_id: str, part_number: int,
                    body: bytes) -> dict:
        with self._lock:
            upload = self.uploads.get(upload_id)
            if upload is None or upload["key"] != object_key:
                raise FakeS3Error("NoSuchUpload")
            if not (1 <= part_number <= 10000):
                raise FakeS3Error("InvalidArgument")
            etag = f'"{sha256_hex(body)[:32]}"'
            # 同 part number 覆盖，与真实 S3 语义一致
            upload["parts"][part_number] = (body, etag.strip('"'))
            return {"etag": etag.strip('"'), "size": len(body)}

    def complete_multipart_upload(self, object_key: str, upload_id: str,
                                  parts: list[dict]) -> str:
        with self._lock:
            upload = self.uploads.get(upload_id)
            if upload is None or upload["key"] != object_key:
                raise FakeS3Error("NoSuchUpload")
            numbers = sorted(p["part_number"] for p in parts)
            if numbers != sorted(upload["parts"].keys()):
                raise FakeS3Error("InvalidPart")
            for p in parts:
                stored = upload["parts"].get(p["part_number"])
                if stored is None or stored[1] != p["etag"]:
                    raise FakeS3Error("InvalidPart")
            merged = b"".join(
                upload["parts"][n][0] for n in sorted(upload["parts"])
            )
            self.objects[object_key] = merged
            del self.uploads[upload_id]
            return sha256_hex(merged)[:32]

    def abort_multipart_upload(self, object_key: str, upload_id: str) -> None:
        with self._lock:
            self.uploads.pop(upload_id, None)

    def list_parts(self, object_key: str, upload_id: str) -> list[dict]:
        with self._lock:
            upload = self.uploads.get(upload_id)
            if upload is None or upload["key"] != object_key:
                return []
            return [
                {"part_number": n, "size": len(b), "etag": etag}
                for n, (b, etag) in sorted(upload["parts"].items())
            ]

    def list_multipart_uploads(self) -> list[dict]:
        with self._lock:
            return [{"object_key": u["key"], "upload_id": uid}
                    for uid, u in self.uploads.items()]

    def head_object(self, object_key: str) -> dict:
        with self._lock:
            data = self.objects.get(object_key)
            if data is None:
                return {}
            return {"size": len(data), "etag": sha256_hex(data)[:32]}

    def delete_object(self, object_key: str) -> None:
        with self._lock:
            self.objects.pop(object_key, None)

    def download_to_file(self, object_key: str, dest_path: str) -> None:
        with self._lock:
            data = self.objects.get(object_key)
            if data is None:
                raise FakeS3Error("NoSuchKey")
            Path(dest_path).write_bytes(data)

    def get_object_stream(self, object_key: str):
        with self._lock:
            data = self.objects.get(object_key)
            if data is None:
                from app.errors import NotFoundError
                raise NotFoundError(f"对象不存在: {object_key}")
            return _FakeStream(data)


@pytest.fixture
def fake_s3(monkeypatch, tmp_path):
    store = FakeS3()
    store.ensure_bucket()
    # settings 为 frozen dataclass，测试中通过 object.__setattr__ 替换路径
    object.__setattr__(db_mod.settings, "db_path", tmp_path / "db.sqlite3")
    object.__setattr__(db_mod.settings, "tmp_dir", tmp_path / "tmp")
    db_mod.init_db(tmp_path / "db.sqlite3")
    return store


@pytest.fixture
def service(fake_s3):
    return UploadService(fake_s3)


def make_ome_tiff(path: Path, *, width: int = 64, height: int = 48,
                  pages: int = 3) -> bytes:
    """生成合法的多页 OME-TIFF（无隐私图像，纯合成数据）。"""
    import numpy as np
    import tifffile

    data = (np.arange(pages * height * width, dtype=np.uint16)
            .reshape(pages, height, width))
    with tifffile.TiffWriter(path) as writer:
        writer.write(data, metadata={"axes": "ZYX"})
    return path.read_bytes()


def split_parts(blob: bytes) -> list[bytes]:
    return [blob[i:i + PART_SIZE] for i in range(0, len(blob), PART_SIZE)]
