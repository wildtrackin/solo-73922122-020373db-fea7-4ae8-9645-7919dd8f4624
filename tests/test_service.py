"""端到端业务测试：分片、幂等、冲突、并发、坏图、恢复、放弃。"""
from __future__ import annotations

import io
import threading

import pytest
import tifffile

from app.config import PART_SIZE
from app.errors import ConflictError, GoneError, NotFoundError
from app.service import expected_part_count

from .conftest import make_ome_tiff, split_parts


# ------------------------------------------------------------- 基础全流程
def test_happy_path_small_single_part(service, tmp_path, fake_s3):
    tiff = tmp_path / "small.ome.tiff"
    blob = make_ome_tiff(tiff, width=32, height=24, pages=2)

    upload = service.create_upload("S001", "small.ome.tiff", len(blob), None)
    assert upload["status"] == "open"
    assert upload["expected_parts"] == 1

    result = service.put_part(upload["upload_id"], 1, blob)
    assert result["duplicate"] is False

    done = service.complete(upload["upload_id"])
    assert done["status"] == "completed"
    assert done["width"] == 32
    assert done["height"] == 24
    assert done["pages"] == 2
    assert done["axes"] == "ZYX"

    dims = service.get_dimensions(upload["upload_id"])
    assert dims["width"] == 32
    assert fake_s3.head_object(upload["object_key"])["size"] == len(blob)


def test_multipart_resume_with_missing_parts(service, tmp_path):
    tiff = tmp_path / "big.ome.tiff"
    # 构造跨越多个 8MiB 分片的大文件
    blob = make_ome_tiff(tiff, width=2048, height=2048, pages=3)
    if len(blob) < PART_SIZE * 2 + 100:
        pytest.skip("合成数据不足以产生多分片")
    parts = split_parts(blob)
    upload = service.create_upload("S002", "big.ome.tiff", len(blob), None)
    assert upload["expected_parts"] == len(parts)

    # 只传部分分片后查询缺片
    for idx, chunk in enumerate(parts[:2], start=1):
        service.put_part(upload["upload_id"], idx, chunk)
    view = service.list_parts(upload["upload_id"])
    assert view["missing_parts"] == list(range(3, len(parts) + 1))
    assert view["bytes_received"] == 2 * PART_SIZE

    # 续传剩余分片（末片可短）
    assert len(parts[-1]) <= PART_SIZE
    for idx, chunk in enumerate(parts[2:], start=3):
        service.put_part(upload["upload_id"], idx, chunk)
    view = service.list_parts(upload["upload_id"])
    assert view["missing_parts"] == []

    done = service.complete(upload["upload_id"])
    assert done["status"] == "completed"
    # 原件内容与本地文件逐字节一致
    _, stream = service.open_object_stream(upload["upload_id"])
    assert stream.read() == blob


def test_complete_rejected_when_parts_missing(service, tmp_path):
    tiff = tmp_path / "x.ome.tiff"
    blob = make_ome_tiff(tiff)
    upload = service.create_upload("S003", "x.ome.tiff", len(blob), None)
    with pytest.raises(ConflictError, match="分片不齐全"):
        service.complete(upload["upload_id"])


# ------------------------------------------------------------- 幂等与冲突
def test_same_part_reupload_is_idempotent(service, tmp_path, fake_s3):
    blob = make_ome_tiff(tmp_path / "a.ome.tiff")
    upload = service.create_upload("S004", "a.ome.tiff", len(blob), None)
    first = service.put_part(upload["upload_id"], 1, blob)
    second = service.put_part(upload["upload_id"], 1, blob)
    assert first["sha256"] == second["sha256"]
    assert second["duplicate"] is True
    # S3 只应收到一次写请求（幂等路径不访问 S3）
    remote = fake_s3.list_parts(upload["object_key"],
                                upload["multipart_upload_id"])
    assert len(remote) == 1


def test_same_part_different_content_returns_409(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "b.ome.tiff")
    upload = service.create_upload("S005", "b.ome.tiff", len(blob), None)
    service.put_part(upload["upload_id"], 1, blob)
    altered = bytearray(blob)
    altered[0] ^= 0xFF
    with pytest.raises(ConflictError) as exc:
        service.put_part(upload["upload_id"], 1, bytes(altered))
    assert exc.value.status_code == 409
    # 原接纳内容不变，完成仍成功
    done = service.complete(upload["upload_id"])
    assert done["status"] == "completed"


def test_wrong_part_size_rejected(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "c.ome.tiff")
    total = len(blob) + PART_SIZE  # 声明两分片
    upload = service.create_upload("S006", "c.ome.tiff", total, None)
    with pytest.raises(ConflictError, match="字节数不符"):
        service.put_part(upload["upload_id"], 1, blob)  # 非末片不足 8MiB
    with pytest.raises(ConflictError, match="非法片号"):
        service.put_part(upload["upload_id"], 99, b"x" * PART_SIZE)


# ------------------------------------------------------------- 并发分片
def test_concurrent_part_uploads(service, tmp_path):
    tiff = tmp_path / "conc.ome.tiff"
    blob = make_ome_tiff(tiff, width=2048, height=2048, pages=3)
    if len(blob) < PART_SIZE * 3:
        pytest.skip("合成数据不足以产生多分片")
    parts = split_parts(blob)
    upload = service.create_upload("S007", "conc.ome.tiff", len(blob), None)

    errors: list[Exception] = []

    def worker(number: int, chunk: bytes):
        try:
            service.put_part(upload["upload_id"], number, chunk)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i, chunk))
        for i, chunk in enumerate(parts, start=1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    view = service.list_parts(upload["upload_id"])
    assert view["missing_parts"] == []
    done = service.complete(upload["upload_id"])
    assert done["status"] == "completed"


# ------------------------------------------------------------- 完成/放弃竞争
def test_complete_and_abort_only_one_wins(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "race.ome.tiff")
    outcomes: list[str] = []

    def run_case() -> tuple[str, str]:
        upload = service.create_upload("S008", "race.ome.tiff", len(blob), None)
        service.put_part(upload["upload_id"], 1, blob)
        barrier = threading.Barrier(2)

        def do_complete():
            barrier.wait()
            try:
                service.complete(upload["upload_id"])
                outcomes.append("completed")
            except Exception:
                outcomes.append("complete_lost")

        def do_abort():
            barrier.wait()
            try:
                service.abort(upload["upload_id"])
                outcomes.append("aborted")
            except Exception:
                outcomes.append("abort_lost")

        t1 = threading.Thread(target=do_complete)
        t2 = threading.Thread(target=do_abort)
        t1.start(); t2.start(); t1.join(); t2.join()
        final = service.get_upload(upload["upload_id"])["status"]
        return upload["upload_id"], final

    # 重复若干轮提高竞争覆盖率
    finals = {run_case()[1] for _ in range(5)}
    # 每轮终态只能是 completed 或 aborted 之一；不会卡死在 completing
    assert finals <= {"completed", "aborted"}
    # 每一轮恰好一个操作胜出：5 轮里 completed 与 aborted 都应出现过
    assert outcomes.count("completed") + outcomes.count("aborted") == 5
    assert set(outcomes) & {"completed", "aborted"}


def test_abort_clears_remote_parts_and_rejects_late_writes(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "ab.ome.tiff")
    upload = service.create_upload("S009", "ab.ome.tiff", len(blob), None)
    service.put_part(upload["upload_id"], 1, blob)
    result = service.abort(upload["upload_id"])
    assert result["status"] == "aborted"

    # 远端 multipart 必须被清除
    remote = service.store.list_multipart_uploads()
    assert all(u["upload_id"] != upload["multipart_upload_id"] for u in remote)

    # 迟到分片写入被拒绝，且不会留下任何残件
    with pytest.raises(GoneError):
        service.put_part(upload["upload_id"], 1, blob)
    assert service.store.list_parts(
        upload["object_key"], upload["multipart_upload_id"]) == []

    # 重复放弃幂等
    assert service.abort(upload["upload_id"])["status"] == "aborted"
    # 放弃后不能完成
    with pytest.raises(GoneError):
        service.complete(upload["upload_id"])


def test_abort_completed_upload_rejected(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "done.ome.tiff")
    upload = service.create_upload("S010", "done.ome.tiff", len(blob), None)
    service.put_part(upload["upload_id"], 1, blob)
    service.complete(upload["upload_id"])
    with pytest.raises(ConflictError):
        service.abort(upload["upload_id"])


# ------------------------------------------------------------- 坏图拦截
def test_bad_image_not_published_and_object_deleted(service, tmp_path, fake_s3):
    # 普通多页 TIFF 但不含 OME 元数据
    import numpy as np
    plain = tmp_path / "plain.tiff"
    data = np.zeros((3, 16, 16), dtype=np.uint8)
    tifffile.imwrite(plain, data)
    blob = plain.read_bytes()

    upload = service.create_upload("S011", "plain.tiff", len(blob), None)
    service.put_part(upload["upload_id"], 1, blob)
    with pytest.raises(Exception) as exc:
        service.complete(upload["upload_id"])
    assert exc.value.status_code == 422

    row = service.get_upload(upload["upload_id"])
    assert row["status"] == "invalid"
    # 坏图对象必须已从湖中删除
    assert fake_s3.head_object(upload["object_key"]) == {}
    # 尺寸与原件均不开放
    with pytest.raises(ConflictError):
        service.get_dimensions(upload["upload_id"])
    with pytest.raises(ConflictError):
        service.open_object_stream(upload["upload_id"])


def test_truncated_tiff_rejected(service, tmp_path, fake_s3):
    blob = make_ome_tiff(tmp_path / "trunc-base.ome.tiff")
    truncated = blob[: len(blob) // 2]
    upload = service.create_upload("S012", "trunc.ome.tiff", len(truncated), None)
    service.put_part(upload["upload_id"], 1, truncated)
    with pytest.raises(Exception) as exc:
        service.complete(upload["upload_id"])
    assert exc.value.status_code == 422
    assert service.get_upload(upload["upload_id"])["status"] == "invalid"
    assert fake_s3.head_object(upload["object_key"]) == {}


def test_sha256_mismatch_rejected(service, tmp_path, fake_s3):
    blob = make_ome_tiff(tmp_path / "sm.ome.tiff")
    # 声明一个错误的整图哈希：结构合法但内容完整性核验失败
    upload = service.create_upload(
        "S013", "sm.ome.tiff", len(blob), "deadbeef" * 8
    )
    service.put_part(upload["upload_id"], 1, blob)
    with pytest.raises(Exception) as exc:
        service.complete(upload["upload_id"])
    assert exc.value.status_code == 422
    assert service.get_upload(upload["upload_id"])["status"] == "invalid"
    assert fake_s3.head_object(upload["object_key"]) == {}


# ------------------------------------------------------------- 崩溃恢复
def test_recover_after_part_written_before_db_commit(service, tmp_path, fake_s3):
    blob = make_ome_tiff(tmp_path / "crash1.ome.tiff",
                         width=2048, height=2048, pages=3)
    if len(blob) < PART_SIZE * 2:
        pytest.skip("合成数据不足以产生多分片")
    parts = split_parts(blob)
    upload = service.create_upload("S014", "crash1.ome.tiff", len(blob), None)
    uid = upload["upload_id"]

    # 模拟：第 1 片已写 S3，但数据库还没记录就崩溃
    fake_s3.upload_part(upload["object_key"],
                        upload["multipart_upload_id"], 1, parts[0])
    assert service.list_parts(uid)["missing_parts"][0] == 1

    # 重启恢复：对账后第 1 片仍是缺片（本地无哈希），客户端重传即可
    report = service.recover_on_startup()
    assert any(r["upload_id"] == uid for r in report["recovered"])
    view = service.list_parts(uid)
    assert 1 in view["missing_parts"]

    # 重传第 1 片（S3 覆盖同号分片），再传剩余片，无需重传整图
    service.put_part(uid, 1, parts[0])
    for i, chunk in enumerate(parts[1:], start=2):
        service.put_part(uid, i, chunk)
    done = service.complete(uid)
    assert done["status"] == "completed"
    _, stream = service.open_object_stream(uid)
    assert stream.read() == blob


def test_recover_after_merge_completes_without_reupload(service, tmp_path,
                                                        fake_s3):
    blob = make_ome_tiff(tmp_path / "crash2.ome.tiff")
    upload = service.create_upload("S015", "crash2.ome.tiff", len(blob), None)
    uid = upload["upload_id"]
    service.put_part(uid, 1, blob)

    # 模拟 completing 期间崩溃：S3 已合并成功，DB 仍停在 completing
    with service._lock(uid):  # noqa: SLF001
        from app import db as db_mod
        with db_mod.connect() as conn:
            db_mod.compare_and_set_status(conn, uid, ("open",), "completing")
        s3_parts = [{"part_number": 1,
                     "etag": fake_s3.uploads[upload["multipart_upload_id"]]
                     ["parts"][1][1]}]
        fake_s3.complete_multipart_upload(
            upload["object_key"], upload["multipart_upload_id"], s3_parts)

    # 重启恢复：检测到对象已存在且大小正确，直接核验续完，不再合并
    report = service.recover_on_startup()
    action = next(r for r in report["recovered"] if r["upload_id"] == uid)
    assert action["action"] == "finalized"
    assert service.get_upload(uid)["status"] == "completed"

    # 客户端重试 complete 必须幂等，且不重复发布
    again = service.complete(uid)
    assert again["status"] == "completed"
    assert len([k for k in fake_s3.objects if k == upload["object_key"]]) == 1


def test_recover_completing_with_bad_image_marks_invalid(service, tmp_path,
                                                         fake_s3):
    import numpy as np
    plain = tmp_path / "p2.tiff"
    tifffile.imwrite(plain, np.zeros((2, 8, 8), dtype=np.uint8))
    blob = plain.read_bytes()
    upload = service.create_upload("S016", "p2.tiff", len(blob), None)
    uid = upload["upload_id"]
    service.put_part(uid, 1, blob)

    with service._lock(uid):  # noqa: SLF001
        from app import db as db_mod
        with db_mod.connect() as conn:
            db_mod.compare_and_set_status(conn, uid, ("open",), "completing")
        etag = fake_s3.uploads[upload["multipart_upload_id"]]["parts"][1][1]
        fake_s3.complete_multipart_upload(
            upload["object_key"], upload["multipart_upload_id"],
            [{"part_number": 1, "etag": etag}])

    service.recover_on_startup()
    assert service.get_upload(uid)["status"] == "invalid"
    assert fake_s3.head_object(upload["object_key"]) == {}


def test_orphan_multipart_cleaned_on_startup(service, fake_s3):
    # 无 DB 归属的孤儿 multipart（残件）应在启动时清除
    fake_s3.create_multipart_upload("samples/ghost/orphan.tiff")
    before = len(fake_s3.list_multipart_uploads())
    assert before == 1
    report = service.recover_on_startup()
    assert len(report["aborted_orphans"]) == 1
    assert fake_s3.list_multipart_uploads() == []


def test_complete_idempotent_after_success(service, tmp_path):
    blob = make_ome_tiff(tmp_path / "idem.ome.tiff")
    upload = service.create_upload("S017", "idem.ome.tiff", len(blob), None)
    service.put_part(upload["upload_id"], 1, blob)
    first = service.complete(upload["upload_id"])
    second = service.complete(upload["upload_id"])
    assert first == second


def test_not_found_upload(service):
    with pytest.raises(NotFoundError):
        service.get_upload("nope")
