"""全局配置：全部通过环境变量注入，便于容器化部署。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 固定分片大小：8 MiB。末片允许短于该值，其余分片必须恰好 8 MiB。
PART_SIZE = 8 * 1024 * 1024

# S3 multipart 合法片号范围
MIN_PART_NUMBER = 1
MAX_PART_NUMBER = 10000

# 裁剪：目标平面解码后的最大字节数（16 MiB），超出即拒绝
MAX_PLANE_BYTES = 16 * 1024 * 1024


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    s3_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    db_path: Path
    tmp_dir: Path
    # 故障注入开关，仅演示环境开启
    enable_faults: bool
    # 启动时执行恢复流程（测试中可关闭）
    recover_on_startup: bool

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("DATA_DIR", "/data"))
        return cls(
            s3_endpoint=_env("S3_ENDPOINT", "http://minio:9000"),
            s3_region=_env("S3_REGION", "us-east-1"),
            s3_access_key=_env("S3_ACCESS_KEY", "minioadmin"),
            s3_secret_key=_env("S3_SECRET_KEY", "minioadmin"),
            s3_bucket=_env("S3_BUCKET", "ome-lake"),
            db_path=Path(_env("DB_PATH", str(data_dir / "db" / "uploads.sqlite3"))),
            tmp_dir=Path(_env("TMP_DIR", str(data_dir / "tmp"))),
            enable_faults=_env("ENABLE_FAULTS", "false").lower() in ("1", "true", "yes"),
            recover_on_startup=_env("RECOVER_ON_STARTUP", "true").lower() in ("1", "true", "yes"),
        )


settings = Settings.from_env()
