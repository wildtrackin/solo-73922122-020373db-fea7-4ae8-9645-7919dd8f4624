"""故障注入：仅在 ENABLE_FAULTS=true 时可用，用于演示崩溃恢复。

注入点刻意选在“远端操作成功、本地状态未更新”的窗口，真实还原
网络中断/进程崩溃造成的状态不一致。
"""
from __future__ import annotations

import os
import signal
import threading

from .config import settings


class FaultInjector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 待触发的故障配置
        self._armed: dict[str, int | None] = {}

    @property
    def enabled(self) -> bool:
        return settings.enable_faults

    def arm(self, fault: str, part_number: int | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._armed = {"fault": fault, "part_number": part_number}  # type: ignore[dict-item]

    def clear(self) -> None:
        with self._lock:
            self._armed = {}

    def status(self) -> dict[str, int | str | None] | None:
        with self._lock:
            return dict(self._armed) if self._armed else None

    def maybe_crash_after_part(self, part_number: int) -> None:
        """第 N 片已写入 S3、尚未写入 SQLite 时崩溃。"""
        with self._lock:
            armed = self._armed.get("fault") == "crash_after_part"
            target = self._armed.get("part_number")
        if armed and (target is None or target == part_number):
            self.clear()
            # SIGTERM：docker restart 策略会把进程重新拉起，等价于主机重启
            os.kill(os.getpid(), signal.SIGTERM)

    def maybe_crash_after_merge(self) -> None:
        """S3 合并已成功、任务尚未标记 completed 时崩溃。"""
        with self._lock:
            armed = self._armed.get("fault") == "crash_after_merge"
        if armed:
            self.clear()
            os.kill(os.getpid(), signal.SIGTERM)


fault_injector = FaultInjector()
