"""业务错误类型与 HTTP 映射。"""
from __future__ import annotations


class ApiError(Exception):
    """所有可预期业务错误的基类，携带 HTTP 状态码与错误码。"""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class ConflictError(ApiError):
    status_code = 409
    code = "conflict"


class GoneError(ApiError):
    status_code = 410
    code = "gone"


class BadImageError(ApiError):
    status_code = 422
    code = "bad_image"


class StorageError(ApiError):
    status_code = 502
    code = "storage_error"


class BadRequestError(ApiError):
    """请求参数本身非法（如轴下标/裁剪矩形越界）。"""

    status_code = 400
    code = "bad_request"


class UnsupportedImageError(ApiError):
    """图像超出裁剪支持范围（多文件/多序列/非灰度/非 uint8-16）。"""

    status_code = 422
    code = "unsupported_image"


class PlaneMappingError(ApiError):
    """OME TiffData 平面映射重叠、越界或不完整，无法唯一定位平面。"""

    status_code = 422
    code = "plane_mapping_ambiguous"


class PlaneReadError(ApiError):
    """湖内对象读取中断或目标平面解码失败。"""

    status_code = 500
    code = "plane_read_failed"
