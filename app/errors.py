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
