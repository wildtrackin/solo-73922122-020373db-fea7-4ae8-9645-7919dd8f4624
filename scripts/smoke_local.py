#!/usr/bin/env python3
"""本地冒烟器：无 Docker 环境下在真实 uvicorn + HTTP 上跑通 demo.py 全流程。

与 Docker 演示的区别：
  - S3 使用 tests.conftest.FakeS3（内存，语义对齐 multipart）；
  - SIGTERM 崩溃点替换为抛异常，随后手动调用启动恢复，
    等价模拟“进程重启 -> recover_on_startup”。
Docker Compose 环境中的 scripts/demo.py 仍走真实 SIGTERM + 容器重启。
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

import numpy as np
import tifffile
import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import main as main_mod  # noqa: E402
from app.faults import fault_injector  # noqa: E402
from scripts import demo  # noqa: E402
from tests.conftest import FakeS3  # noqa: E402


def generate_samples(out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    large = (np.arange(3 * 2048 * 2048, dtype=np.uint16).reshape(3, 2048, 2048)
             + rng.integers(0, 64, size=(3, 2048, 2048), dtype=np.uint16))
    with tifffile.TiffWriter(out / "good-large.ome.tiff") as w:
        w.write(large, photometric="minisblack", metadata={"axes": "ZYX"})
    small = rng.integers(0, 256, size=(256, 256), dtype=np.uint8)
    with tifffile.TiffWriter(out / "good-small.ome.tiff") as w:
        w.write(small, photometric="minisblack", metadata={"axes": "YX"})
    tzc = (np.arange(2 * 3 * 2 * 512 * 384, dtype=np.uint16)
           .reshape(2, 3, 2, 512, 384)
           + rng.integers(0, 128, size=(2, 3, 2, 512, 384), dtype=np.uint16))
    with tifffile.TiffWriter(out / "good-tzc.ome.tiff", ome=True) as w:
        w.write(tzc, photometric="minisblack",
                metadata={"axes": "TZCYX",
                          "Channel": {"Name": ["DAPI", "GFP"]},
                          "PhysicalSizeX": 0.325, "PhysicalSizeXUnit": "µm",
                          "PhysicalSizeY": 0.325, "PhysicalSizeYUnit": "µm"})
    tifffile.imwrite(out / "bad-no-ome.tiff",
                     np.zeros((2, 64, 64), dtype=np.uint8),
                     photometric="minisblack")


class SilentCrash(RuntimeError):
    """模拟进程在故障注入点被 SIGTERM。"""


def main() -> None:
    work = ROOT / ".smoke"
    sample_dir = work / "sample-data"
    generate_samples(sample_dir)

    fake = FakeS3()
    fake.ensure_bucket()

    # 指向隔离的临时 SQLite / tmp
    object.__setattr__(main_mod.settings, "db_path", work / "db.sqlite3")
    object.__setattr__(main_mod.settings, "tmp_dir", work / "tmp")
    object.__setattr__(main_mod.settings, "enable_faults", True)
    object.__setattr__(main_mod.settings, "recover_on_startup", False)
    main_mod.S3Store = lambda: fake  # type: ignore[assignment]

    # 故障点改为抛异常（不杀进程），由下面手动恢复模拟重启
    def fake_crash_after_part(_n: int) -> None:
        if fault_injector.status() is not None:
            fault_injector.clear()
            raise SilentCrash("simulated crash after part upload")

    def fake_crash_after_merge() -> None:
        if fault_injector.status() is not None:
            fault_injector.clear()
            raise SilentCrash("simulated crash after merge")

    fault_injector.maybe_crash_after_part = fake_crash_after_part  # type: ignore[assignment]
    fault_injector.maybe_crash_after_merge = fake_crash_after_merge  # type: ignore[assignment]

    config = uvicorn.Config(main_mod.app, host="127.0.0.1", port=8011,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    # demo.py 环境接线
    os.environ["API_BASE"] = "http://127.0.0.1:8011"
    os.environ["SAMPLE_DIR"] = str(sample_dir)
    demo.API = os.environ["API_BASE"]
    demo.SAMPLE_DIR = sample_dir

    # “等待重启”替换为：直接执行启动恢复流程
    def fake_wait(_timeout: float = 60.0) -> None:
        main_mod.service.recover_on_startup()

    demo.wait_api_ready = fake_wait  # type: ignore[assignment]

    demo.s3_inflight_uploads = lambda: [  # type: ignore[assignment]
        {"Key": u["object_key"], "UploadId": u["upload_id"]}
        for u in fake.list_multipart_uploads()
    ]

    def fake_head(key: str) -> dict:
        head = fake.head_object(key)
        return {"ContentLength": head["size"]} if head else {}

    demo.s3_head = fake_head  # type: ignore[assignment]

    try:
        demo.main()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    print("\n[smoke] 全部场景在本地真实 HTTP 服务上验证通过")


if __name__ == "__main__":
    main()
