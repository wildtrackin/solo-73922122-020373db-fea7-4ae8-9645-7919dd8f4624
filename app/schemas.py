"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PositiveInt

# 上传任务生命周期状态
UploadStatus = Literal["open", "completing", "completed", "aborted", "invalid"]


class CreateUploadRequest(BaseModel):
    sample_id: str = Field(..., min_length=1, max_length=200, description="样本号")
    filename: str = Field(..., min_length=1, max_length=255, description="原始文件名，仅作记录")
    total_bytes: PositiveInt = Field(..., description="整图声明总字节数")
    content_sha256: str | None = Field(
        None, pattern=r"^[0-9a-fA-F]{64}$",
        description="可选：整图 SHA256（64 位十六进制），提供则校验")


class UploadResponse(BaseModel):
    upload_id: str
    sample_id: str
    filename: str
    object_key: str
    total_bytes: int
    part_size: int
    expected_parts: int
    status: UploadStatus


class PartInfo(BaseModel):
    part_number: int
    size: int
    sha256: str
    etag: str


class PartsResponse(BaseModel):
    upload_id: str
    status: UploadStatus
    part_size: int
    expected_parts: int
    received_parts: list[PartInfo]
    missing_parts: list[int]
    bytes_received: int


class CompleteResponse(BaseModel):
    upload_id: str
    status: UploadStatus
    object_key: str | None = None
    width: int | None = None
    height: int | None = None
    pages: int | None = None
    dtype: str | None = None
    axes: str | None = None


class AbortResponse(BaseModel):
    upload_id: str
    status: Literal["aborted"]


class DimensionsResponse(BaseModel):
    upload_id: str
    sample_id: str
    object_key: str
    width: int
    height: int
    pages: int
    dtype: str
    axes: str


class FaultArmRequest(BaseModel):
    # crash_after_part：第 N 片写入 S3 后、落库前崩溃，演示中途崩溃恢复
    # crash_after_merge：S3 合并成功后、标记完成前崩溃，演示合并后回执丢失
    fault: Literal["crash_after_part", "crash_after_merge"]
    part_number: int | None = None


class CropRequest(BaseModel):
    """焦层局部视野裁剪参数：T/Z/C 逻辑下标 + X/Y 像素矩形。"""

    t: int = Field(0, ge=0, description="T 逻辑下标（缺失轴仅接受 0）")
    z: int = Field(0, ge=0, description="Z 逻辑下标（焦层，缺失轴仅接受 0）")
    c: int = Field(0, ge=0, description="C 逻辑下标（通道，缺失轴仅接受 0）")
    x: int = Field(..., ge=0, description="裁剪矩形左上角 X（像素）")
    y: int = Field(..., ge=0, description="裁剪矩形左上角 Y（像素）")
    width: int = Field(..., ge=1, description="裁剪宽度（像素）")
    height: int = Field(..., ge=1, description="裁剪高度（像素）")


class CropResponse(BaseModel):
    crop_id: str
    upload_id: str
    object_key: str
    t: int
    z: int
    c: int
    x: int
    y: int
    width: int
    height: int
    dtype: str
    size_bytes: int
    sha256: str
    download_url: str
