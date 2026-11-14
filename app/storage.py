"""对象存储适配层：基于 boto3 的真实 S3 multipart upload（MinIO 兼容）。"""
from __future__ import annotations

import hashlib
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import settings
from .errors import StorageError


def _s3_error_code(exc: Exception) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None


class ObjectStore(Protocol):
    def bucket_exists(self) -> bool: ...
    def ensure_bucket(self) -> None: ...
    def create_multipart_upload(self, object_key: str) -> str: ...
    def upload_part(self, object_key: str, upload_id: str, part_number: int,
                    body: bytes) -> dict[str, Any]: ...
    def complete_multipart_upload(self, object_key: str, upload_id: str,
                                  parts: list[dict[str, Any]]) -> str: ...
    def abort_multipart_upload(self, object_key: str, upload_id: str) -> None: ...
    def list_parts(self, object_key: str, upload_id: str) -> list[dict[str, Any]]: ...
    def list_multipart_uploads(self) -> list[dict[str, Any]]: ...
    def head_object(self, object_key: str) -> dict[str, Any]: ...
    def put_object(self, object_key: str, data: bytes,
                   content_type: str = "image/tiff") -> None: ...
    def delete_object(self, object_key: str) -> None: ...
    def download_to_file(self, object_key: str, dest_path: str) -> None: ...
    def get_object_stream(self, object_key: str) -> Any: ...


class S3Store:
    """MinIO/S3 实现。所有异常统一转换为 StorageError，便于上层映射。"""

    def __init__(self) -> None:
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        self.bucket = settings.s3_bucket

    def bucket_exists(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError:
            return False

    def ensure_bucket(self) -> None:
        if self.bucket_exists():
            return
        try:
            self.client.create_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if _s3_error_code(exc) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise StorageError(f"创建存储桶失败: {exc}") from exc

    def create_multipart_upload(self, object_key: str) -> str:
        try:
            resp = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=object_key,
                ContentType="image/tiff",
            )
            return resp["UploadId"]
        except ClientError as exc:
            raise StorageError(f"初始化 multipart 上传失败: {exc}") from exc

    def upload_part(self, object_key: str, upload_id: str, part_number: int,
                    body: bytes) -> dict[str, Any]:
        try:
            resp = self.client.upload_part(
                Bucket=self.bucket, Key=object_key,
                UploadId=upload_id, PartNumber=part_number, Body=body,
            )
            etag = (resp.get("ETag") or "").strip('"')
            return {"etag": etag, "size": len(body)}
        except ClientError as exc:
            raise StorageError(f"分片 {part_number} 写入失败: {exc}") from exc

    def complete_multipart_upload(self, object_key: str, upload_id: str,
                                  parts: list[dict[str, Any]]) -> str:
        s3_parts = [
            {"PartNumber": p["part_number"], "ETag": f'"{p["etag"]}"'}
            for p in sorted(parts, key=lambda x: x["part_number"])
        ]
        try:
            resp = self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=object_key,
                UploadId=upload_id, MultipartUpload={"Parts": s3_parts},
            )
            return (resp.get("ETag") or "").strip('"')
        except ClientError as exc:
            raise StorageError(f"合并 multipart 上传失败: {exc}") from exc

    def abort_multipart_upload(self, object_key: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(
                Bucket=self.bucket, Key=object_key, UploadId=upload_id,
            )
        except ClientError as exc:
            # 已经不存在视为成功（幂等放弃）
            if _s3_error_code(exc) not in ("NoSuchUpload", "NotFound", "404"):
                raise StorageError(f"放弃 multipart 上传失败: {exc}") from exc

    def list_parts(self, object_key: str, upload_id: str) -> list[dict[str, Any]]:
        try:
            parts: list[dict[str, Any]] = []
            paginator = self.client.get_paginator("list_parts")
            for page in paginator.paginate(Bucket=self.bucket, Key=object_key,
                                           UploadId=upload_id):
                for p in page.get("Parts", []):
                    parts.append({
                        "part_number": p["PartNumber"],
                        "size": p["Size"],
                        "etag": (p.get("ETag") or "").strip('"'),
                    })
            return parts
        except ClientError as exc:
            if _s3_error_code(exc) in ("NoSuchUpload", "NotFound", "404"):
                return []
            raise StorageError(f"查询远端分片失败: {exc}") from exc

    def list_multipart_uploads(self) -> list[dict[str, Any]]:
        try:
            uploads: list[dict[str, Any]] = []
            paginator = self.client.get_paginator("list_multipart_uploads")
            for page in paginator.paginate(Bucket=self.bucket):
                for u in page.get("Uploads", []) or []:
                    uploads.append({
                        "object_key": u["Key"],
                        "upload_id": u["UploadId"],
                    })
            return uploads
        except ClientError as exc:
            raise StorageError(f"列举 multipart 上传失败: {exc}") from exc

    def head_object(self, object_key: str) -> dict[str, Any]:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=object_key)
            return {"size": resp.get("ContentLength", 0),
                    "etag": (resp.get("ETag") or "").strip('"')}
        except ClientError as exc:
            if _s3_error_code(exc) in ("404", "NotFound", "NoSuchKey"):
                return {}
            raise StorageError(f"查询对象失败: {exc}") from exc

    def delete_object(self, object_key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=object_key)
        except ClientError as exc:
            raise StorageError(f"删除对象失败: {exc}") from exc

    def put_object(self, object_key: str, data: bytes,
                   content_type: str = "image/tiff") -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=object_key, Body=data,
                ContentType=content_type,
            )
        except ClientError as exc:
            raise StorageError(f"裁片对象写入失败: {exc}") from exc

    def download_to_file(self, object_key: str, dest_path: str) -> None:
        try:
            self.client.download_file(self.bucket, object_key, dest_path)
        except ClientError as exc:
            raise StorageError(f"下载对象失败: {exc}") from exc

    def get_object_stream(self, object_key: str) -> Any:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=object_key)
            return resp["Body"]
        except ClientError as exc:
            if _s3_error_code(exc) in ("404", "NotFound", "NoSuchKey"):
                from .errors import NotFoundError
                raise NotFoundError(f"对象不存在: {object_key}") from exc
            raise StorageError(f"读取对象失败: {exc}") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
