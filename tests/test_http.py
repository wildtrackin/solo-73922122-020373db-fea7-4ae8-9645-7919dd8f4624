"""HTTP 层测试：完整请求/响应语义与状态码。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import PART_SIZE
from app.image_validation import validate_ome_tiff  # noqa: F401  确保应用可导入

from .conftest import make_ome_tiff, split_parts


@pytest.fixture
def client(fake_s3, monkeypatch):
    object.__setattr__(main_mod.settings, "recover_on_startup", False)
    monkeypatch.setattr(main_mod, "S3Store", lambda: fake_s3)
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def uploaded(client, tmp_path):
    blob = make_ome_tiff(tmp_path / "h.ome.tiff")
    resp = client.post("/uploads", json={
        "sample_id": "SHTTP", "filename": "h.ome.tiff",
        "total_bytes": len(blob),
    })
    assert resp.status_code == 201
    upload = resp.json()
    r = client.put(f"/uploads/{upload['upload_id']}/parts/1", content=blob)
    assert r.status_code == 200
    return upload, blob


def test_health_and_openapi(client):
    assert client.get("/health").json()["status"] == "ok"
    spec = client.get("/openapi.json").json()
    assert "/uploads" in spec["paths"]


def test_full_flow_over_http(client, uploaded):
    upload, blob = uploaded
    uid = upload["upload_id"]

    # 完成前原件/尺寸不开放
    assert client.get(f"/uploads/{uid}/dimensions").status_code == 409
    assert client.get(f"/uploads/{uid}/object").status_code == 409

    r = client.post(f"/uploads/{uid}/complete")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["width"] == 64 and body["height"] == 48 and body["pages"] == 3

    # 完成后开放查询，原件逐字节一致
    d = client.get(f"/uploads/{uid}/dimensions")
    assert d.status_code == 200 and d.json()["axes"] == "ZYX"
    obj = client.get(f"/uploads/{uid}/object")
    assert obj.status_code == 200
    assert obj.content == blob
    assert obj.headers["content-type"] == "image/tiff"


def test_part_idempotency_and_conflict_http(client, uploaded):
    upload, blob = uploaded
    uid = upload["upload_id"]
    r1 = client.put(f"/uploads/{uid}/parts/1", content=blob)
    assert r1.status_code == 200 and r1.json()["duplicate"] is True

    altered = bytearray(blob)
    altered[10] ^= 0x01
    r2 = client.put(f"/uploads/{uid}/parts/1", content=bytes(altered))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "part_conflict"


def test_missing_parts_listing_and_resume_http(client, tmp_path):
    blob = make_ome_tiff(tmp_path / "big.ome.tiff",
                         width=2048, height=2048, pages=3)
    parts = split_parts(blob)
    upload = client.post("/uploads", json={
        "sample_id": "SHTTP2", "filename": "big.ome.tiff",
        "total_bytes": len(blob)}).json()
    uid = upload["upload_id"]
    assert upload["expected_parts"] == len(parts)

    client.put(f"/uploads/{uid}/parts/1", content=parts[0])
    view = client.get(f"/uploads/{uid}/parts").json()
    assert view["missing_parts"] == list(range(2, len(parts) + 1))

    # 并发续传剩余分片（用线程池模拟并发请求）
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(client.put, f"/uploads/{uid}/parts/{i}", content=chunk)
            for i, chunk in enumerate(parts[1:], start=2)
        ]
        assert all(f.result().status_code == 200 for f in futures)

    assert client.get(f"/uploads/{uid}/parts").json()["missing_parts"] == []
    r = client.post(f"/uploads/{uid}/complete")
    assert r.status_code == 200
    assert client.get(f"/uploads/{uid}/object").content == blob


def test_complete_with_missing_parts_409(client, tmp_path):
    blob = make_ome_tiff(tmp_path / "m.ome.tiff")
    upload = client.post("/uploads", json={
        "sample_id": "SHTTP3", "filename": "m.ome.tiff",
        "total_bytes": len(blob)}).json()
    r = client.post(f"/uploads/{upload['upload_id']}/complete")
    assert r.status_code == 409


def test_abort_flow_http(client, uploaded):
    upload, blob = uploaded
    uid = upload["upload_id"]
    assert client.post(f"/uploads/{uid}/abort").status_code == 200
    # 迟到写入 410
    late = client.put(f"/uploads/{uid}/parts/1", content=blob)
    assert late.status_code == 410
    # 放弃后完成 410，重复放弃仍 200
    assert client.post(f"/uploads/{uid}/complete").status_code == 410
    assert client.post(f"/uploads/{uid}/abort").status_code == 200


def test_bad_image_rejected_http(client, tmp_path):
    import numpy as np
    import tifffile
    plain = tmp_path / "no-ome.tiff"
    tifffile.imwrite(plain, np.zeros((2, 8, 8), dtype=np.uint8))
    blob = plain.read_bytes()
    upload = client.post("/uploads", json={
        "sample_id": "SHTTP4", "filename": "no-ome.tiff",
        "total_bytes": len(blob)}).json()
    client.put(f"/uploads/{upload['upload_id']}/parts/1", content=blob)
    r = client.post(f"/uploads/{upload['upload_id']}/complete")
    assert r.status_code == 422
    assert client.get(f"/uploads/{upload['upload_id']}").json()["status"] == "invalid"


def test_sample_listing_and_404(client, uploaded):
    upload, _ = uploaded
    rows = client.get(f"/samples/{upload['sample_id']}/uploads").json()
    assert len(rows) == 1 and rows[0]["sample_id"] == upload["sample_id"]
    assert client.get("/uploads/does-not-exist").status_code == 404


def test_fault_endpoint_disabled_by_default(client):
    r = client.post("/dev/faults", json={"fault": "crash_after_merge"})
    assert r.status_code == 403
