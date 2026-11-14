"""FastAPI 应用：路由与 HTTP 映射。

路由先在 async 上下文中读取请求体（框架在线程池处理阻塞 IO），
再调用同步的 UploadService；同一 upload_id 的业务串行化由服务层锁保证。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import PART_SIZE, settings
from .db import init_db
from .errors import ApiError
from .faults import fault_injector
from .schemas import (
    AbortResponse,
    CompleteResponse,
    CreateUploadRequest,
    DimensionsResponse,
    FaultArmRequest,
    PartsResponse,
    UploadResponse,
)
from .service import UploadService
from .storage import S3Store

service: UploadService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    init_db()
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    store = S3Store()
    # MinIO 容器可能稍后就绪：带重试地确保存储桶存在
    last_exc: Exception | None = None
    for attempt in range(30):
        try:
            store.ensure_bucket()
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 - 启动期网络重试
            last_exc = exc
            time.sleep(2)
    if last_exc is not None:
        raise RuntimeError(f"无法连接对象存储 {settings.s3_endpoint}") from last_exc

    service = UploadService(store)
    if settings.recover_on_startup:
        service.recover_on_startup()
    yield


app = FastAPI(
    title="OME-TIFF 断点续传入湖 API",
    version="1.0.0",
    description=(
        "按样本号创建上传，8 MiB 分片续传，真实 S3 multipart；"
        "合并后核验 OME 元数据与 TIFF 页目录，坏图不得入湖。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _upload_response(row: dict) -> dict:
    return {
        "upload_id": row["upload_id"],
        "sample_id": row["sample_id"],
        "filename": row["filename"],
        "object_key": row["object_key"],
        "total_bytes": row["total_bytes"],
        "part_size": row["part_size"],
        "expected_parts": row["expected_parts"],
        "status": row["status"],
    }


@app.post("/uploads", response_model=UploadResponse, status_code=201,
          tags=["上传生命周期"])
async def create_upload(payload: CreateUploadRequest):
    """按样本号创建续传任务，声明总字节数，返回分片规划。"""
    row = service.create_upload(
        sample_id=payload.sample_id,
        filename=payload.filename,
        total_bytes=payload.total_bytes,
        content_sha256=payload.content_sha256,
    )
    return _upload_response(row)


@app.get("/uploads/{upload_id}", response_model=UploadResponse,
         tags=["上传生命周期"])
async def get_upload(upload_id: str):
    return _upload_response(service.get_upload(upload_id))


@app.get("/samples/{sample_id}/uploads", response_model=list[UploadResponse],
         tags=["上传生命周期"])
async def list_sample_uploads(sample_id: str):
    return [_upload_response(r) for r in service.list_by_sample(sample_id)]


@app.get("/uploads/{upload_id}/parts", response_model=PartsResponse,
         tags=["分片"])
async def list_parts(upload_id: str):
    """查询已接纳片号与缺片清单，供断点续传决策。"""
    return service.list_parts(upload_id)


@app.put("/uploads/{upload_id}/parts/{part_number}",
         tags=["分片"])
async def put_part(upload_id: str, part_number: int, request: Request):
    """上传单个分片（body 即原始字节）。

    - 同片同字节重传：幂等返回 200；
    - 同片异内容：409，不覆盖已接纳分片；
    - 非末片必须恰好 8 MiB，末片按总字节数取余。
    """
    body = await request.body()
    result = service.put_part(upload_id, part_number, body)
    return result


@app.post("/uploads/{upload_id}/complete", response_model=CompleteResponse,
          tags=["上传生命周期"])
async def complete_upload(upload_id: str):
    """分片齐全后合并并核验；重复调用幂等，不重复发布。"""
    return service.complete(upload_id)


@app.post("/uploads/{upload_id}/abort", response_model=AbortResponse,
          tags=["上传生命周期"])
async def abort_upload(upload_id: str):
    """放弃上传：清除远端分片，迟到分片写入一律拒绝。"""
    return service.abort(upload_id)


@app.get("/uploads/{upload_id}/dimensions",
         response_model=DimensionsResponse, tags=["原件查询"])
async def get_dimensions(upload_id: str):
    """仅 completed 任务开放：返回图像尺寸等元数据。"""
    return service.get_dimensions(upload_id)


@app.get("/uploads/{upload_id}/object", tags=["原件查询"])
async def download_object(upload_id: str):
    """仅 completed 任务开放：流式读取湖内完整原件。"""
    upload, body = service.open_object_stream(upload_id)

    def iter_stream():
        try:
            for chunk in body.iter_chunks(chunk_size=PART_SIZE):
                yield chunk
        finally:
            body.close()

    safe_name = (
        upload["filename"].replace("\\", "_").replace('"', "'")
        .replace("\r", " ").replace("\n", " ")
    )
    return StreamingResponse(
        iter_stream(),
        media_type="image/tiff",
        headers={
            "Content-Length": str(upload["total_bytes"]),
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )


@app.get("/health", tags=["运维"])
async def health():
    return {"status": "ok", "part_size": PART_SIZE,
            "faults_enabled": fault_injector.enabled}


# ------------------------------------------------------------ 故障注入（演示）
@app.post("/dev/faults", tags=["故障注入（仅演示环境）"])
async def arm_fault(payload: FaultArmRequest):
    if not fault_injector.enabled:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "faults_disabled",
                               "message": "未开启 ENABLE_FAULTS"}},
        )
    fault_injector.arm(payload.fault, payload.part_number)
    return {"armed": fault_injector.status()}


@app.get("/dev/faults", tags=["故障注入（仅演示环境）"])
async def get_fault():
    if not fault_injector.enabled:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "faults_disabled",
                               "message": "未开启 ENABLE_FAULTS"}},
        )
    return {"armed": fault_injector.status()}


@app.delete("/dev/faults", tags=["故障注入（仅演示环境）"])
async def clear_fault():
    if not fault_injector.enabled:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "faults_disabled",
                               "message": "未开启 ENABLE_FAULTS"}},
        )
    fault_injector.clear()
    return {"armed": None}
