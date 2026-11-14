"""OME-TIFF 焦层视野裁剪：按 OME 轴与 TiffData 映射定位平面，裁出二维 OME-TIFF。

支持范围（超出即明确报错，绝不返回残缺裁片）：
- 单文件数据集：TiffData 不通过 UUID/FileName 引用外部文件；
- 单图像序列：OME-XML 仅一个 Image，且 tifffile 只识别出一个 series；
- 灰度图：每个 Channel 的 SamplesPerPixel 均为 1；
- 像素类型 uint8 / uint16；
- 目标平面解码后不超过 16 MiB（MAX_PLANE_BYTES）；
- SizeZ/SizeC/SizeT 为 1 的“缺失轴”仅接受下标 0。

平面定位严格依据 OME-XML 的 TiffData 映射（FirstZ/FirstC/FirstT/IFD/
PlaneCount 与 DimensionOrder）。映射重叠、越界或不完整一律视为含糊并报错。
原图只读，裁片另写新文件。
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile

from .config import MAX_PLANE_BYTES
from .errors import (
    BadRequestError,
    PlaneMappingError,
    PlaneReadError,
    UnsupportedImageError,
)

# OME 像素类型 -> (numpy dtype 字符串, 每像素字节数)
SUPPORTED_TYPES: dict[str, tuple[str, int]] = {
    "uint8": ("uint8", 1),
    "uint16": ("uint16", 2),
}

_VALID_DIMENSION_ORDERS = frozenset(
    {"XYZCT", "XYZTC", "XYCZT", "XYCTZ", "XYTZC", "XYTCZ"}
)


@dataclass
class OmeModel:
    """从 OME-XML 解析出的、裁剪所需的全部信息。"""

    size_x: int
    size_y: int
    size_z: int
    size_c: int
    size_t: int
    dimension_order: str
    dtype: str  # numpy dtype 字符串（uint8/uint16）
    bytes_per_pixel: int
    channel_names: list[str | None]
    physical_size_x: float | None
    physical_size_x_unit: str | None
    physical_size_y: float | None
    physical_size_y_unit: str | None
    plane_map: dict[tuple[int, int, int], int]  # (z, c, t) -> TIFF 页号(IFD)


@dataclass
class CropResult:
    width: int
    height: int
    dtype: str
    size_bytes: int
    sha256: str


# --------------------------------------------------------------------- OME 解析
def _child(parent: ET.Element, tag: str) -> ET.Element | None:
    return parent.find(f"{{*}}{tag}")


def _children(parent: ET.Element, tag: str) -> list[ET.Element]:
    return parent.findall(f"{{*}}{tag}")


def _parse_int(raw: str | None, name: str) -> int:
    try:
        return int(str(raw), 10)
    except (TypeError, ValueError):
        raise PlaneMappingError(f"OME-XML 属性 {name} 非法: {raw!r}") from None


def _parse_physical_size(pixels: ET.Element, attr: str,
                         unit_attr: str) -> tuple[float | None, str | None]:
    """物理像素尺寸：缺失或非法时按“无该元数据”处理，不阻断裁剪。"""
    raw = pixels.get(attr)
    if raw is None:
        return None, None
    try:
        value = float(raw)
    except ValueError:
        return None, None
    if value <= 0:
        return None, None
    return value, pixels.get(unit_attr)


def parse_ome_model(ome_xml: str, page_count: int) -> OmeModel:
    """解析 OME-XML 并构建 (z, c, t) -> IFD 的唯一映射。"""
    try:
        root = ET.fromstring(ome_xml)
    except ET.ParseError as exc:
        raise PlaneMappingError(f"OME-XML 解析失败: {exc}") from exc

    images = _children(root, "Image")
    if len(images) != 1:
        raise UnsupportedImageError(
            f"仅支持单图像序列，OME 元数据中含 {len(images)} 个 Image"
        )
    pixels = _child(images[0], "Pixels")
    if pixels is None:
        raise PlaneMappingError("OME-XML 缺少 Pixels 元素")
    if _child(pixels, "MetadataOnly") is not None:
        raise UnsupportedImageError("OME 声明 MetadataOnly，像素数据不在本文件")

    def required_size(attr: str) -> int:
        value = _parse_int(pixels.get(attr), attr)
        if value < 1:
            raise PlaneMappingError(f"OME-XML {attr} 必须为正整数: {value}")
        return value

    size_x = required_size("SizeX")
    size_y = required_size("SizeY")
    size_z = required_size("SizeZ")
    size_c = required_size("SizeC")
    size_t = required_size("SizeT")

    ome_type = pixels.get("Type") or ""
    if ome_type not in SUPPORTED_TYPES:
        raise UnsupportedImageError(
            f"仅支持灰度 uint8/uint16，OME Type 为 {ome_type or '缺失'}"
        )
    dtype, bytes_per_pixel = SUPPORTED_TYPES[ome_type]

    dimension_order = pixels.get("DimensionOrder") or "XYZCT"
    if dimension_order not in _VALID_DIMENSION_ORDERS:
        raise PlaneMappingError(f"非法 DimensionOrder: {dimension_order}")

    # 通道：SamplesPerPixel 必须为 1（灰度），数量须与 SizeC 一致
    channels = _children(pixels, "Channel")
    for channel in channels:
        spp = channel.get("SamplesPerPixel")
        if spp not in (None, "1"):
            raise UnsupportedImageError(
                f"仅支持灰度图（SamplesPerPixel=1），通道实际为 {spp}"
            )
    if len(channels) != size_c:
        raise PlaneMappingError(
            f"OME Channel 元素数 {len(channels)} 与 SizeC={size_c} 不一致，"
            "通道映射含糊"
        )
    channel_names: list[str | None] = []
    for channel in channels:
        name = channel.get("Name")
        channel_names.append(name if name else None)

    plane_map = _build_plane_map(
        pixels, size_z, size_c, size_t, dimension_order, page_count
    )
    psx, psx_unit = _parse_physical_size(pixels, "PhysicalSizeX",
                                         "PhysicalSizeXUnit")
    psy, psy_unit = _parse_physical_size(pixels, "PhysicalSizeY",
                                         "PhysicalSizeYUnit")
    return OmeModel(
        size_x=size_x, size_y=size_y, size_z=size_z, size_c=size_c,
        size_t=size_t, dimension_order=dimension_order, dtype=dtype,
        bytes_per_pixel=bytes_per_pixel, channel_names=channel_names,
        physical_size_x=psx, physical_size_x_unit=psx_unit,
        physical_size_y=psy, physical_size_y_unit=psy_unit,
        plane_map=plane_map,
    )


def _build_plane_map(
    pixels: ET.Element,
    size_z: int,
    size_c: int,
    size_t: int,
    dimension_order: str,
    page_count: int,
) -> dict[tuple[int, int, int], int]:
    """按 TiffData 元素构建平面映射，任何含糊都明确报错。

    规则：
    - 无 TiffData 元素：默认单块 IFD=0 起、按 DimensionOrder 排满全部平面；
    - PlaneCount 缺失时：唯一元素视为覆盖剩余全部平面，多元素视为单平面块
      （Bio-Formats 逐平面写法）；
    - 坐标越界、IFD 越界、块间平面/IFD 重叠、覆盖不完整 -> PlaneMappingError。
    """
    total_planes = size_z * size_c * size_t
    order = dimension_order[2:]  # 例 "CZT"：由最快到最慢
    sizes = {"Z": size_z, "C": size_c, "T": size_t}

    def to_linear(z: int, c: int, t: int) -> int:
        idx = {"Z": z, "C": c, "T": t}
        linear = 0
        mult = 1
        for dim in order:
            linear += idx[dim] * mult
            mult *= sizes[dim]
        return linear

    def from_linear(linear: int) -> tuple[int, int, int]:
        idx: dict[str, int] = {}
        for dim in order:
            idx[dim] = linear % sizes[dim]
            linear //= sizes[dim]
        return idx["Z"], idx["C"], idx["T"]

    tiff_datas = _children(pixels, "TiffData")
    # 每块: (起始 IFD, FirstZ, FirstC, FirstT, PlaneCount)
    blocks: list[tuple[int, int, int, int, int]] = []
    if not tiff_datas:
        blocks.append((0, 0, 0, 0, total_planes))
    else:
        for td in tiff_datas:
            uuid_el = _child(td, "UUID")
            if uuid_el is not None and uuid_el.get("FileName"):
                raise UnsupportedImageError(
                    "多文件 OME 数据集（TiffData 引用外部文件 "
                    f"{uuid_el.get('FileName')!r}），仅支持单文件"
                )
            ifd = _parse_int(td.get("IFD") or "0", "IFD")
            first_z = _parse_int(td.get("FirstZ") or "0", "FirstZ")
            first_c = _parse_int(td.get("FirstC") or "0", "FirstC")
            first_t = _parse_int(td.get("FirstT") or "0", "FirstT")
            raw_count = td.get("PlaneCount")
            if raw_count is not None:
                count = _parse_int(raw_count, "PlaneCount")
            elif len(tiff_datas) == 1:
                count = total_planes - to_linear(first_z, first_c, first_t)
            else:
                count = 1
            blocks.append((ifd, first_z, first_c, first_t, count))

    plane_map: dict[tuple[int, int, int], int] = {}
    used_ifds: set[int] = set()
    for ifd, first_z, first_c, first_t, count in blocks:
        if count < 1:
            raise PlaneMappingError("TiffData PlaneCount 必须为正整数")
        for name, value, size in (("FirstZ", first_z, size_z),
                                  ("FirstC", first_c, size_c),
                                  ("FirstT", first_t, size_t)):
            if not 0 <= value < size:
                raise PlaneMappingError(
                    f"TiffData {name}={value} 越界（Size{name[-1]}={size}）"
                )
        if ifd < 0:
            raise PlaneMappingError("TiffData IFD 不能为负")
        start = to_linear(first_z, first_c, first_t)
        if start + count > total_planes:
            raise PlaneMappingError(
                "TiffData 平面坐标越界：PlaneCount 超出剩余平面数"
            )
        if ifd + count > page_count:
            raise PlaneMappingError(
                f"TiffData 引用 IFD {ifd}..{ifd + count - 1}，"
                f"超出文件实际页数 {page_count}"
            )
        for offset in range(count):
            coord = from_linear(start + offset)
            target_ifd = ifd + offset
            if coord in plane_map:
                raise PlaneMappingError(
                    f"平面 (Z={coord[0]}, C={coord[1]}, T={coord[2]}) "
                    "被多个 TiffData 块重复映射，映射含糊"
                )
            if target_ifd in used_ifds:
                raise PlaneMappingError(
                    f"IFD {target_ifd} 被多个平面重复引用，映射含糊"
                )
            plane_map[coord] = target_ifd
            used_ifds.add(target_ifd)

    if len(plane_map) != total_planes:
        raise PlaneMappingError(
            f"TiffData 映射不完整：{total_planes} 个平面仅覆盖 "
            f"{len(plane_map)} 个，无法唯一定位"
        )
    return plane_map


# --------------------------------------------------------------------- 参数校验
def _check_axis(name: str, index: int, size: int) -> None:
    if index < 0:
        raise BadRequestError(
            f"{name} 轴下标不能为负: {index}", code="axis_index_out_of_range"
        )
    if size == 1 and index != 0:
        raise BadRequestError(
            f"{name} 轴缺失（Size{name}=1），仅接受下标 0，收到 {index}",
            code="axis_index_out_of_range",
        )
    if index >= size:
        raise BadRequestError(
            f"{name} 轴下标越界：Size{name}={size}，收到 {index}",
            code="axis_index_out_of_range",
        )


# --------------------------------------------------------------------- 裁剪主流程
def crop_ome_tiff(src_path: Path, out_path: Path, *, t: int, z: int, c: int,
                  x: int, y: int, width: int, height: int) -> CropResult:
    """从 src_path 定位并解码 (t, z, c) 平面，裁出矩形区域写入 out_path。

    原图只读不改；任何一步失败都抛出明确异常，不产生残缺裁片文件。
    """
    try:
        tif = tifffile.TiffFile(str(src_path))
    except Exception as exc:  # tifffile 解析异常类型多样，统一归类
        raise PlaneReadError(
            f"无法打开湖内原件（读取中断或文件损坏）: {exc}"
        ) from exc
    try:
        # 注意：必须先访问 tif.pages 再访问 tif.series——series 构建会把
        # 未加载页替换为惰性 TiffFrame（缺少 samplesperpixel 等属性）。
        page_count = len(tif.pages)
        ome_xml = tif.ome_metadata
        if not ome_xml:
            raise PlaneMappingError("湖内对象缺少 OME 元数据，无法定位平面")
        model = parse_ome_model(ome_xml, page_count)

        # 轴下标：缺失轴（Size=1）仅接受 0，其余须在范围内
        _check_axis("T", t, model.size_t)
        _check_axis("Z", z, model.size_z)
        _check_axis("C", c, model.size_c)

        # 裁剪矩形必须完整落在图像内
        if x >= model.size_x or y >= model.size_y \
                or x + width > model.size_x or y + height > model.size_y:
            raise BadRequestError(
                f"裁剪区域越界：图像宽 {model.size_x} 高 {model.size_y}，"
                f"请求 x={x} y={y} 宽={width} 高={height}",
                code="crop_out_of_bounds",
            )

        # 平面解码体积守卫（解码前按元数据计算，超限不解码）
        plane_bytes = model.size_x * model.size_y * model.bytes_per_pixel
        if plane_bytes > MAX_PLANE_BYTES:
            raise UnsupportedImageError(
                f"目标平面解码后 {plane_bytes} 字节，"
                f"超过 16 MiB 上限（{MAX_PLANE_BYTES} 字节）",
                code="plane_too_large",
            )

        ifd = model.plane_map[(z, c, t)]
        page = tif.pages[ifd]
        if page.samplesperpixel != 1:
            raise UnsupportedImageError(
                f"目标页 SamplesPerPixel={page.samplesperpixel}，非灰度图"
            )
        if str(page.dtype) != model.dtype:
            raise PlaneMappingError(
                f"OME Type={model.dtype} 与页 dtype={page.dtype} 不一致"
            )
        if tuple(page.shape) != (model.size_y, model.size_x):
            raise PlaneMappingError(
                f"页形状 {tuple(page.shape)} 与 OME SizeX/SizeY="
                f"{model.size_x}x{model.size_y} 不一致"
            )

        try:
            plane = page.asarray()
        except Exception as exc:  # 截断/损坏的条带在解码时抛出
            raise PlaneReadError(
                f"目标平面解码失败（读取中断或数据损坏）: {exc}"
            ) from exc
        if plane.shape != (model.size_y, model.size_x) \
                or str(plane.dtype) != model.dtype:
            raise PlaneReadError(
                "解码结果与 OME 元数据不符，拒绝返回残缺裁片"
            )

        # numpy 切片精确对应请求矩形；拷贝为连续数组，与源文件句柄解耦
        cropped = np.ascontiguousarray(plane[y:y + height, x:x + width])
        channel_name = (
            model.channel_names[c] if c < len(model.channel_names) else None
        )

        # 单图像序列校验（放在解码后：访问 series 会使页对象惰性化）
        if len(tif.series) != 1:
            raise UnsupportedImageError(
                f"仅支持单图像序列，文件含 {len(tif.series)} 个图像序列"
            )
    finally:
        tif.close()

    metadata: dict = {"axes": "YX"}
    if channel_name:
        metadata["Channel"] = {"Name": channel_name}
    if model.physical_size_x is not None:
        metadata["PhysicalSizeX"] = model.physical_size_x
        if model.physical_size_x_unit:
            metadata["PhysicalSizeXUnit"] = model.physical_size_x_unit
    if model.physical_size_y is not None:
        metadata["PhysicalSizeY"] = model.physical_size_y
        if model.physical_size_y_unit:
            metadata["PhysicalSizeYUnit"] = model.physical_size_y_unit

    try:
        with tifffile.TiffWriter(str(out_path), ome=True) as writer:
            writer.write(cropped, photometric="minisblack", metadata=metadata)
    except Exception as exc:
        raise PlaneReadError(f"裁片写出失败: {exc}") from exc

    data = out_path.read_bytes()
    return CropResult(
        width=width, height=height, dtype=model.dtype,
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
    )
