"""上传业务逻辑：并发控制、幂等分片、完成核验、放弃与崩溃恢复。"""
from __future__ import annotations

import math
import threading
import uuid
from pathlib import Path
from typing import Any

from . import db
from .config import MAX_PART_NUMBER, PART_SIZE, settings
from .errors import ConflictError, GoneError, NotFoundError, StorageError
from .faults import fault_injector
from .image_validation import validate_ome_tiff
from .storage import S3Store, sha256_hex

# 每个 upload_id 一把可重入锁：同上传的分片/完成/放弃串行化，
# 不同上传互不阻塞。单进程内有效，符合题目“单进程 API”约束。
_locks_guard = threading.Lock()
_upload_locks: dict[str, threading.RLock] = {}


def _upload_lock(upload_id: str) -> threading.RLock:
    with _locks_guard:
        lock = _upload_locks.get(upload_id)
        if lock is None:
            lock = threading.RLock()
            _upload_locks[upload_id] = lock
        return lock


def expected_part_count(total_bytes: int) -> int:
    return math.ceil(total_bytes / PART_SIZE)


def expected_part_size(total_bytes: int, part_number: int, expected_parts: int) -> int:
    if part_number == expected_parts:
        remainder = total_bytes % PART_SIZE
        return remainder if remainder else PART_SIZE
    return PART_SIZE


class UploadService:
    def __init__(self, store: S3Store | Any):
        self.store = store

    @staticmethod
    def _lock(upload_id: str) -> threading.RLock:
        return _upload_lock(upload_id)

    # ------------------------------------------------------------------ 创建
    def create_upload(self, sample_id: str, filename: str, total_bytes: int,
                      content_sha256: str | None) -> dict[str, Any]:
        if total_bytes <= 0:
            raise ConflictError("total_bytes 必须为正整数")
        expected_parts = expected_part_count(total_bytes)
        if expected_parts > MAX_PART_NUMBER:
            raise ConflictError(
                f"分片数 {expected_parts} 超过 S3 上限 {MAX_PART_NUMBER}"
            )

        upload_id = uuid.uuid4().hex
        # 对象键：样本号前缀，样本号内按上传 ID 隔离，避免重名覆盖
        safe_sample = sample_id.strip("/").replace("..", "_")
        object_key = f"samples/{safe_sample}/{upload_id}/{filename}"

        # 先向 S3 申请 multipart upload，再落库；落库失败则立刻清理远端
        multipart_upload_id = self.store.create_multipart_upload(object_key)
        try:
            with db.connect() as conn:
                conn.execute(
                    """INSERT INTO uploads (upload_id, sample_id, filename, object_key,
                           total_bytes, content_sha256, multipart_upload_id, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
                    (upload_id, sample_id, filename, object_key, total_bytes,
                     content_sha256, multipart_upload_id),
                )
        except Exception:
            self.store.abort_multipart_upload(object_key, multipart_upload_id)
            raise

        return self.get_upload(upload_id)

    # ------------------------------------------------------------------ 查询
    def get_upload(self, upload_id: str) -> dict[str, Any]:
        with db.connect() as conn:
            row = db.get_upload(conn, upload_id)
        if row is None:
            raise NotFoundError(f"上传任务不存在: {upload_id}")
        row["part_size"] = PART_SIZE
        row["expected_parts"] = expected_part_count(row["total_bytes"])
        return row

    def list_parts(self, upload_id: str) -> dict[str, Any]:
        upload = self.get_upload(upload_id)
        with db.connect() as conn:
            parts = db.list_parts(conn, upload_id)
        expected_parts = upload["expected_parts"]
        received = {p["part_number"] for p in parts}
        missing = [n for n in range(1, expected_parts + 1) if n not in received]
        return {
            "upload_id": upload_id,
            "status": upload["status"],
            "part_size": PART_SIZE,
            "expected_parts": expected_parts,
            "received_parts": parts,
            "missing_parts": missing,
            "bytes_received": sum(p["size"] for p in parts),
        }

    def list_by_sample(self, sample_id: str) -> list[dict[str, Any]]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM uploads WHERE sample_id=? ORDER BY created_at",
                (sample_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["part_size"] = PART_SIZE
            item["expected_parts"] = expected_part_count(item["total_bytes"])
            result.append(item)
        return result

    # -------------------------------------------------------------- 分片上传
    def put_part(self, upload_id: str, part_number: int, body: bytes) -> dict[str, Any]:
        lock = _upload_lock(upload_id)
        with lock:
            with db.connect() as conn:
                upload = db.get_upload(conn, upload_id)
                if upload is None:
                    raise NotFoundError(f"上传任务不存在: {upload_id}")
                if upload["status"] in ("aborted", "invalid"):
                    raise GoneError(
                        f"任务已 {upload['status']}，拒绝迟到的分片写入",
                        code="upload_terminated",
                    )
                if upload["status"] in ("completing", "completed"):
                    raise ConflictError(
                        f"任务状态为 {upload['status']}，不再接受分片"
                    )

                total_bytes = upload["total_bytes"]
                expected_parts = expected_part_count(total_bytes)
                if not (1 <= part_number <= expected_parts):
                    raise ConflictError(
                        f"非法片号 {part_number}，合法范围 1..{expected_parts}"
                    )

                want_size = expected_part_size(total_bytes, part_number, expected_parts)
                if len(body) != want_size:
                    raise ConflictError(
                        f"分片 {part_number} 字节数不符：要求 {want_size}，"
                        f"实际 {len(body)}（非末片必须恰好 8 MiB）"
                    )

                digest = sha256_hex(body)
                existing = conn.execute(
                    "SELECT size, sha256 FROM parts WHERE upload_id=? AND part_number=?",
                    (upload_id, part_number),
                ).fetchone()
                if existing is not None:
                    if existing["sha256"] == digest and existing["size"] == len(body):
                        # 同片同内容：幂等返回，不再请求 S3
                        return {"part_number": part_number, "size": len(body),
                                "sha256": digest, "duplicate": True}
                    # 同片异内容：明确 409，绝不覆盖已接纳分片
                    raise ConflictError(
                        f"分片 {part_number} 已存在且内容不同（SHA256 不一致）",
                        code="part_conflict",
                    )

                object_key = upload["object_key"]
                multipart_id = upload["multipart_upload_id"]

            # S3 写分片放在数据库事务外（网络 IO 不持事务）
            result = self.store.upload_part(
                object_key, multipart_id, part_number, body
            )

            # 故障注入：远端已写、本地未落库时崩溃
            fault_injector.maybe_crash_after_part(part_number)

            with db.connect() as conn:
                # 二次确认：崩溃恢复/并发放弃可能已改变任务状态
                upload = db.get_upload(conn, upload_id)
                if upload is None or upload["status"] in ("aborted", "invalid"):
                    # 任务已终止：尽力清理刚写入的远端分片，不留残件
                    self.store.abort_multipart_upload(object_key, multipart_id)
                    raise GoneError("任务已终止，远端分片已清理",
                                    code="upload_terminated")
                db.upsert_part(conn, upload_id, part_number, len(body),
                               digest, result["etag"])
                db.touch_upload(conn, upload_id)

            return {"part_number": part_number, "size": len(body),
                    "sha256": digest, "etag": result["etag"], "duplicate": False}

    # ------------------------------------------------------------------ 完成
    def complete(self, upload_id: str) -> dict[str, Any]:
        lock = _upload_lock(upload_id)
        with lock:
            with db.connect() as conn:
                upload = db.get_upload(conn, upload_id)
                if upload is None:
                    raise NotFoundError(f"上传任务不存在: {upload_id}")
                status = upload["status"]

                if status == "completed":
                    # 回执丢失后的重试：幂等返回已发布原件，不重复合并/发布
                    return self._build_completed_response(upload)
                if status == "aborted":
                    raise GoneError("任务已放弃，无法完成", code="upload_terminated")
                if status == "invalid":
                    raise ConflictError("图像校验失败，任务已标记为 invalid")
                if status == "completing":
                    # 上次处理中崩溃（或有并发完成请求）：走恢复式收尾
                    return self._finalize(upload_id)

                parts = db.list_parts(conn, upload_id)
                self._assert_all_parts_present(upload, parts)

                # CAS：open -> completing，与放弃操作竞争，只有一个生效
                changed = db.compare_and_set_status(
                    conn, upload_id, ("open",), "completing"
                )
            if changed == 0:
                # 被并发放弃抢先：重新读状态给出确定结论
                fresh = self.get_upload(upload_id)
                if fresh["status"] == "aborted":
                    raise GoneError("并发放弃已生效，无法完成",
                                    code="upload_terminated")
                return self._finalize(upload_id)

            return self._finalize(upload_id)

    def _finalize(self, upload_id: str) -> dict[str, Any]:
        """completing 状态下的幂等收尾：合并 -> 下载核验 -> 发布/回滚。"""
        with db.connect() as conn:
            upload = db.get_upload(conn, upload_id)
            if upload is None:
                raise NotFoundError(f"上传任务不存在: {upload_id}")
            if upload["status"] == "completed":
                return self._build_completed_response(upload)
            if upload["status"] not in ("completing", "open"):
                raise ConflictError(f"任务状态 {upload['status']} 无法收尾")
            parts = db.list_parts(conn, upload_id)
            self._assert_all_parts_present(upload, parts)
            object_key = upload["object_key"]
            multipart_id = upload["multipart_upload_id"]
            total_bytes = upload["total_bytes"]
            content_sha256 = upload["content_sha256"]

        head = self.store.head_object(object_key)
        if head and head.get("size") == total_bytes:
            # 合并已成功（典型：complete_multipart_upload 之后进程崩溃/回执丢失）
            # 不重新合并、不重传任何分片，直接进入核验续完
            pass
        else:
            s3_parts = [
                {"part_number": p["part_number"], "etag": p["etag"]}
                for p in parts
            ]
            self.store.complete_multipart_upload(object_key, multipart_id, s3_parts)
            head = self.store.head_object(object_key)
            if not head or head.get("size") != total_bytes:
                raise StorageError(
                    "合并后对象大小与声明不符，拒绝发布"
                )

        # 故障注入：合并成功、标记完成前崩溃
        fault_injector.maybe_crash_after_merge()

        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.tmp_dir / f"{upload_id}.tiff"
        try:
            self.store.download_to_file(object_key, str(tmp_path))
            dimensions = validate_ome_tiff(
                tmp_path, total_bytes,
                content_sha256.lower() if content_sha256 else None,
            )
        except Exception:
            # 坏图不得入湖：删除远端对象并标记 invalid，后续查询明确失败
            self.store.delete_object(object_key)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE uploads SET status='invalid', error_message=?, "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE upload_id=?",
                    ("OME-TIFF 校验失败，远端对象已删除", upload_id),
                )
            raise
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        with db.connect() as conn:
            conn.execute(
                """UPDATE uploads SET status='completed', width=?, height=?, pages=?,
                       dtype=?, axes=?, error_message=NULL,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE upload_id=? AND status IN ('completing','open')""",
                (dimensions.width, dimensions.height, dimensions.pages,
                 dimensions.dtype, dimensions.axes, upload_id),
            )
            upload = db.get_upload(conn, upload_id)
        return self._build_completed_response(upload)

    # ------------------------------------------------------------------ 放弃
    def abort(self, upload_id: str) -> dict[str, Any]:
        lock = _upload_lock(upload_id)
        with lock:
            with db.connect() as conn:
                upload = db.get_upload(conn, upload_id)
                if upload is None:
                    raise NotFoundError(f"上传任务不存在: {upload_id}")
                status = upload["status"]
                if status == "aborted":
                    return {"upload_id": upload_id, "status": "aborted"}
                if status == "completed":
                    raise ConflictError("任务已完成并入湖，不能放弃")
                if status == "invalid":
                    raise ConflictError("任务已因坏图标记 invalid，无需放弃")

                # CAS：open/completing -> aborted，与完成竞争，只有一个生效
                changed = db.compare_and_set_status(
                    conn, upload_id, ("open", "completing"), "aborted"
                )
                object_key = upload["object_key"]
                multipart_id = upload["multipart_upload_id"]

            if changed == 0:
                fresh = self.get_upload(upload_id)
                if fresh["status"] == "completed":
                    raise ConflictError("并发完成已生效，不能放弃")
                if fresh["status"] == "aborted":
                    return {"upload_id": upload_id, "status": "aborted"}
                raise ConflictError(f"状态冲突，当前状态: {fresh['status']}")

            # 状态先落库为 aborted：此后任何迟到分片在入口即被拒绝；
            # 再清除远端 multipart 分片（含已上传的全部 part）
            self.store.abort_multipart_upload(object_key, multipart_id)
            # 防御性清理：若恰有合并产物也删除，确保无残件
            if self.store.head_object(object_key):
                self.store.delete_object(object_key)
            return {"upload_id": upload_id, "status": "aborted"}

    # -------------------------------------------------------------- 原件/尺寸
    def get_dimensions(self, upload_id: str) -> dict[str, Any]:
        upload = self.get_upload(upload_id)
        if upload["status"] != "completed":
            raise ConflictError(
                f"任务状态为 {upload['status']}，尺寸尚未开放查询",
                code="not_ready",
            )
        return {
            "upload_id": upload_id,
            "sample_id": upload["sample_id"],
            "object_key": upload["object_key"],
            "width": upload["width"],
            "height": upload["height"],
            "pages": upload["pages"],
            "dtype": upload["dtype"],
            "axes": upload["axes"],
        }

    def open_object_stream(self, upload_id: str):
        upload = self.get_upload(upload_id)
        if upload["status"] != "completed":
            raise ConflictError(
                f"任务状态为 {upload['status']}，原件尚未开放下载",
                code="not_ready",
            )
        return upload, self.store.get_object_stream(upload["object_key"])

    # ------------------------------------------------------------------ 恢复
    def recover_on_startup(self) -> dict[str, Any]:
        """进程启动时核实并续完所有未决任务。"""
        report: dict[str, Any] = {"recovered": [], "aborted_orphans": []}
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM uploads WHERE status IN ('open','completing')"
            ).fetchall()
            pending = [dict(r) for r in rows]

        for upload in pending:
            upload_id = upload["upload_id"]
            with _upload_lock(upload_id):
                try:
                    self._reconcile_remote_parts(upload)
                    with db.connect() as conn:
                        fresh = db.get_upload(conn, upload_id)
                    if fresh is None:
                        continue
                    if fresh["status"] == "completing":
                        self._finalize(upload_id)
                        report["recovered"].append(
                            {"upload_id": upload_id, "action": "finalized"})
                    else:
                        report["recovered"].append(
                            {"upload_id": upload_id, "action": "resumable"})
                except Exception as exc:  # noqa: BLE001
                    # 单任务失败不阻断启动：保留原状态，客户端可稍后重试
                    import logging
                    logging.getLogger("app.recovery").exception(
                        "启动恢复失败 upload_id=%s: %s", upload_id, exc
                    )
                    report["recovered"].append(
                        {"upload_id": upload_id, "action": "failed",
                         "error": str(exc)})

        # 清理无数据库归属的孤儿 multipart 上传（残件）
        known = {(u["object_key"], u["multipart_upload_id"])
                 for u in pending if u["multipart_upload_id"]}
        for remote in self.store.list_multipart_uploads():
            token = (remote["object_key"], remote["upload_id"])
            if token not in known:
                self.store.abort_multipart_upload(
                    remote["object_key"], remote["upload_id"]
                )
                report["aborted_orphans"].append(remote)
        return report

    def _reconcile_remote_parts(self, upload: dict[str, Any]) -> None:
        """对齐 S3 已接纳分片与本地记录。

        覆盖“分片已写 S3、落库前崩溃”：
        - 本地与远端都有的分片，核对 ETag/大小后保留；
        - 仅远端存在的分片，本地没有内容哈希无法核验，不登记为已接纳，
          由 GET parts 作为缺片返回；客户端重传同内容分片时 S3 覆盖
          对应 part number，走正常接纳路径，无需重传整图；
        - 仅本地存在的分片保留（远端 list_parts 短暂不一致时由 S3 重试兜底）。
        """
        upload_id = upload["upload_id"]
        remote_parts = self.store.list_parts(
            upload["object_key"], upload["multipart_upload_id"]
        )
        if not remote_parts:
            return
        remote_map = {p["part_number"]: p for p in remote_parts}
        with db.connect() as conn:
            local = db.list_parts(conn, upload_id)
            for local_part in local:
                remote = remote_map.get(local_part["part_number"])
                if remote is None:
                    continue
                if local_part["etag"] != remote["etag"] or \
                        local_part["size"] != remote["size"]:
                    conn.execute(
                        "UPDATE parts SET etag=?, size=? WHERE upload_id=? AND part_number=?",
                        (remote["etag"], remote["size"], upload_id,
                         local_part["part_number"]),
                    )

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _assert_all_parts_present(upload: dict[str, Any],
                                  parts: list[dict[str, Any]]) -> None:
        total_bytes = upload["total_bytes"]
        expected_parts = expected_part_count(total_bytes)
        if len(parts) != expected_parts:
            missing = expected_parts - len(parts)
            raise ConflictError(
                f"分片不齐全：应有 {expected_parts} 片，实到 {len(parts)} 片，"
                f"缺 {missing} 片，不能完成"
            )
        seen = {p["part_number"] for p in parts}
        if seen != set(range(1, expected_parts + 1)):
            raise ConflictError("片号不连续，不能完成")
        total = sum(p["size"] for p in parts)
        if total != total_bytes:
            raise ConflictError(
                f"分片字节合计 {total} 与声明总字节 {total_bytes} 不符"
            )

    @staticmethod
    def _build_completed_response(upload: dict[str, Any]) -> dict[str, Any]:
        return {
            "upload_id": upload["upload_id"],
            "status": "completed",
            "object_key": upload["object_key"],
            "width": upload["width"],
            "height": upload["height"],
            "pages": upload["pages"],
            "dtype": upload["dtype"],
            "axes": upload["axes"],
        }
