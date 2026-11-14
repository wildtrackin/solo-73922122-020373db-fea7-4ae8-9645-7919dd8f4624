"""OME-TIFF 校验与尺寸提取。

入湖门槛（坏图不得入湖）：
1. 文件可被 tifffile 打开，TIFF 页目录（IFD）完整可遍历；
2. 包含 OME 元数据（ImageDescription 中的 OME-XML）；
3. 每页都可解析出形状与数据类型；
4. 声明总字节数与实际文件大小一致；
5. 若创建时提供整图 SHA256，则哈希必须匹配。

只做结构性核验与元数据解析，不全量解码像素，避免大文件校验过慢。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import tifffile

from .errors import BadImageError


@dataclass
class ImageDimensions:
    width: int
    height: int
    pages: int
    dtype: str
    axes: str


def _looks_like_ome(description: str | None) -> bool:
    if not description:
        return False
    head = description.lstrip()[:200]
    return "OME:" in head or "<OME" in head or "<ome:" in head


def validate_ome_tiff(path: Path, expected_size: int,
                      expected_sha256: str | None = None) -> ImageDimensions:
    if not path.exists():
        raise BadImageError("合并后的临时文件不存在")

    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise BadImageError(
            f"字节数不符：声明 {expected_size}，实际合并对象 {actual_size}"
        )

    try:
        tif = tifffile.TiffFile(str(path))
    except Exception as exc:  # tifffile 抛多种解析异常，统一归类
        raise BadImageError(f"无法解析 TIFF 结构：{exc}") from exc

    try:
        if not tif.pages:
            raise BadImageError("TIFF 不含任何页目录（IFD）")

        ome_found = False
        page_shapes: list[tuple[int, ...]] = []
        dtypes: set[str] = set()

        # 逐页访问 IFD，截断/损坏的页目录会在此处抛出异常
        for page in tif.pages:
            desc = page.description
            if _looks_like_ome(desc):
                ome_found = True
            # 访问 shape/dtype 会触发 IFD 字段解析，损坏页直接报错
            shape = tuple(page.shape)
            if any(dim <= 0 for dim in shape):
                raise BadImageError(f"页目录含非法维度: {shape}")
            page_shapes.append(shape)
            dtypes.add(str(page.dtype))

        # OME 元数据可能只出现在首页，也可能存在于 tif.ome_metadata
        if not ome_found and not _looks_like_ome(getattr(tif, "ome_metadata", None)):
            raise BadImageError("缺少 OME-XML 元数据，不是 OME-TIFF")

        series = tif.series[0]
        axes = "".join(series.axes) if series.axes else "".join(tif.series[0].get_axes())
        shape = tuple(series.shape)

        # 维度约定：最后两个轴为 Y(高)、X(宽)，与 OME/TIFF 习惯一致
        if len(shape) < 2:
            raise BadImageError(f"图像维度不足，无法提取宽高: shape={shape}")
        height, width = int(shape[-2]), int(shape[-1])
        dtype = str(series.dtype)
        pages = len(tif.pages)
    except BadImageError:
        raise
    except Exception as exc:
        raise BadImageError(f"TIFF 页目录或 OME 元数据损坏: {exc}") from exc
    finally:
        tif.close()

    if expected_sha256:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256.lower():
            raise BadImageError("整图 SHA256 校验失败，对象内容与声明不一致")

    return ImageDimensions(
        width=width, height=height, pages=pages, dtype=dtype, axes=axes,
    )
