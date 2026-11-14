#!/usr/bin/env python3
"""生成演示用合成 OME-TIFF（纯算法数据，无任何隐私图像）。

产物：
  good-large.ome.tiff  多页大文件（约 24 MiB，演示 8 MiB 分片续传）
  good-small.ome.tiff  单页小文件（末片短分片）
  good-tzc.ome.tiff    多维文件（T=2 Z=3 C=2，含通道名与物理像素尺寸，
                       演示焦层视野裁剪）
  bad-no-ome.tiff      普通 TIFF，缺 OME 元数据（演示坏图拦截）
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import tifffile

OUT_DIR = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/sample-data")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(42)

    # 3 页 2048x2048 uint16：确定性梯度 + 噪声，约 24 MiB，4 个分片
    large = (
        np.arange(3 * 2048 * 2048, dtype=np.uint16).reshape(3, 2048, 2048)
        + rng.integers(0, 64, size=(3, 2048, 2048), dtype=np.uint16)
    )
    with tifffile.TiffWriter(OUT_DIR / "good-large.ome.tiff") as w:
        w.write(large, photometric="minisblack", metadata={"axes": "ZYX"})

    # 小文件：单页 256x256 uint8，仅 64 KiB，单分片（末片可短）
    small = rng.integers(0, 256, size=(256, 256), dtype=np.uint8)
    with tifffile.TiffWriter(OUT_DIR / "good-small.ome.tiff") as w:
        w.write(small, photometric="minisblack", metadata={"axes": "YX"})

    # 多维文件：T=2 Z=3 C=2，512x384 uint16，含通道名与物理像素尺寸
    tzc = (
        np.arange(2 * 3 * 2 * 512 * 384, dtype=np.uint16)
        .reshape(2, 3, 2, 512, 384)
        + rng.integers(0, 128, size=(2, 3, 2, 512, 384), dtype=np.uint16)
    )
    with tifffile.TiffWriter(OUT_DIR / "good-tzc.ome.tiff", ome=True) as w:
        w.write(
            tzc,
            photometric="minisblack",
            metadata={
                "axes": "TZCYX",
                "Channel": {"Name": ["DAPI", "GFP"]},
                "PhysicalSizeX": 0.325,
                "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": 0.325,
                "PhysicalSizeYUnit": "µm",
            },
        )

    # 坏图：结构合法的 TIFF，但不含 OME-XML
    tifffile.imwrite(
        OUT_DIR / "bad-no-ome.tiff",
        np.zeros((2, 64, 64), dtype=np.uint8),
        photometric="minisblack",
    )

    for path in sorted(OUT_DIR.glob("*.tiff")):
        print(f"generated {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
