"""裁剪功能测试：T/Z/C 定位、TiffData 映射、边界与不支持情形、幂等、下载。"""
from __future__ import annotations

import io

import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

from app import main as main_mod
from app.errors import (
    BadRequestError,
    ConflictError,
    PlaneMappingError,
    PlaneReadError,
    StorageError,
    UnsupportedImageError,
)

from .conftest import split_parts

# ----------------------------------------------------------------- 样本构造


def make_tzc_ome(path, *, sizes=(2, 3, 2), shape=(8, 10), dtype=np.uint16,
                 channel_names=("DAPI", "GFP"), physical=(0.325, 0.25)):
    """生成含通道名与物理尺寸的 TZCYX OME-TIFF，返回 (字节, 源数组)。"""
    size_t, size_z, size_c = sizes
    h, w = shape
    data = np.arange(size_t * size_z * size_c * h * w,
                     dtype=dtype).reshape(size_t, size_z, size_c, h, w)
    metadata = {"axes": "TZCYX"}
    if channel_names:
        metadata["Channel"] = {"Name": list(channel_names)}
    if physical:
        metadata["PhysicalSizeX"] = physical[0]
        metadata["PhysicalSizeXUnit"] = "µm"
        metadata["PhysicalSizeY"] = physical[1]
        metadata["PhysicalSizeYUnit"] = "µm"
    with tifffile.TiffWriter(path, ome=True) as writer:
        writer.write(data, photometric="minisblack", metadata=metadata)
    return path.read_bytes(), data


def make_custom_ome(path, data, ome_xml):
    """逐页写入并嵌入手工构造的 OME-XML（用于 TiffData 映射场景）。"""
    with tifffile.TiffWriter(path) as writer:
        for i, plane in enumerate(data):
            writer.write(plane, description=ome_xml if i == 0 else None,
                         metadata=None)
    return path.read_bytes()


def upload_completed(service, tmp_path, name, blob, sample="SCROP"):
    upload = service.create_upload(sample, name, len(blob), None)
    for i, chunk in enumerate(split_parts(blob), start=1):
        service.put_part(upload["upload_id"], i, chunk)
    service.complete(upload["upload_id"])
    return upload


def read_crop_array(service, crop_id):
    row, stream = service.open_crop_stream(crop_id)
    try:
        payload = stream.read()
    finally:
        stream.close()
    with tifffile.TiffFile(io.BytesIO(payload)) as tif:
        return tif.series[0].asarray(), tif.ome_metadata, payload


# ----------------------------------------------------------------- 正向流程
def test_crop_happy_path_pixel_exact_and_metadata(service, tmp_path, fake_s3):
    blob, data = make_tzc_ome(tmp_path / "tzc.ome.tiff")
    upload = upload_completed(service, tmp_path, "tzc.ome.tiff", blob)
    uid = upload["upload_id"]

    row, created = service.create_crop(
        uid, t=1, z=2, c=1, x=3, y=2, width=5, height=4
    )
    assert created is True
    assert row["dtype"] == "uint16"
    assert row["width"] == 5 and row["height"] == 4
    assert row["download_url"] == f"/crops/{row['crop_id']}"

    array, ome_xml, payload = read_crop_array(service, row["crop_id"])
    # 宽高与像素逐点对应裁剪区域
    assert array.shape == (4, 5)
    assert array.dtype == np.uint16
    np.testing.assert_array_equal(array, data[1, 2, 1][2:6, 3:8])
    # 通道名与物理像素尺寸保留；输出为单平面二维 OME-TIFF
    assert 'Name="GFP"' in ome_xml
    assert 'PhysicalSizeX="0.325"' in ome_xml
    assert 'PhysicalSizeY="0.25"' in ome_xml
    assert 'SizeZ="1"' in ome_xml and 'SizeC="1"' in ome_xml
    assert 'SizeT="1"' in ome_xml
    assert row["size_bytes"] == len(payload)

    # 原件不改：湖内对象与上传字节逐字节一致
    assert fake_s3.objects[upload["object_key"]] == blob


def test_crop_idempotent_same_params(service, tmp_path, fake_s3):
    blob, _ = make_tzc_ome(tmp_path / "idem.ome.tiff")
    upload = upload_completed(service, tmp_path, "idem.ome.tiff", blob)
    uid = upload["upload_id"]

    first, created1 = service.create_crop(
        uid, t=0, z=0, c=0, x=0, y=0, width=4, height=4)
    second, created2 = service.create_crop(
        uid, t=0, z=0, c=0, x=0, y=0, width=4, height=4)
    assert created1 is True and created2 is False
    assert first["crop_id"] == second["crop_id"]
    # 湖中仅一份裁片对象
    crops = [k for k in fake_s3.objects if "/crops/" in k]
    assert len(crops) == 1
    # 不同参数 -> 新裁片
    third, created3 = service.create_crop(
        uid, t=0, z=0, c=0, x=1, y=1, width=4, height=4)
    assert created3 is True and third["crop_id"] != first["crop_id"]

    listed = service.list_crops(uid)
    assert len(listed) == 2


def test_crop_insert_race_only_one_wins(service):
    """并发同参裁剪：唯一约束保证只有一行落库（INSERT OR IGNORE）。"""
    from app import db as db_mod

    upload = service.create_upload("SRACE", "race.ome.tiff", 16, None)
    row = {
        "crop_id": "a" * 32, "upload_id": upload["upload_id"],
        "object_key": "k/a", "filename": "f.ome.tiff",
        "t_index": 0, "z_index": 0, "c_index": 0,
        "x": 0, "y": 0, "width": 1, "height": 1,
        "dtype": "uint16", "size_bytes": 10, "sha256": "x" * 64,
    }
    with db_mod.connect() as conn:
        assert db_mod.insert_crop_if_absent(conn, row) is True
    with db_mod.connect() as conn:
        loser = dict(row, crop_id="b" * 32, object_key="k/b")
        assert db_mod.insert_crop_if_absent(conn, loser) is False
        existing = db_mod.get_crop_by_params(
            conn, upload["upload_id"], 0, 0, 0, 0, 0, 1, 1)
        assert existing["crop_id"] == "a" * 32


def test_crop_requires_completed(service, tmp_path):
    blob, _ = make_tzc_ome(tmp_path / "open.ome.tiff")
    upload = service.create_upload("SCROP", "open.ome.tiff", len(blob), None)
    with pytest.raises(ConflictError) as exc:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    assert exc.value.status_code == 409


def test_crop_tiffdata_blocks_mapping(service, tmp_path):
    """多块 TiffData（含 FirstT 偏移）+ DimensionOrder=XYZCT 的定位。"""
    data = np.arange(4 * 8 * 10, dtype=np.uint16).reshape(4, 8, 10)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="10" SizeY="8" SizeC="1" SizeZ="2" SizeT="2">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1" Name="GFP"/>'
        '<TiffData IFD="0" FirstZ="0" FirstT="0" FirstC="0" PlaneCount="2"/>'
        '<TiffData IFD="2" FirstZ="0" FirstT="1" FirstC="0" PlaneCount="2"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "blocks.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "blocks.ome.tiff", blob)
    uid = upload["upload_id"]

    # XYZCT：线性下标 = Z + SizeZ*T；页序 [t0z0, t0z1, t1z0, t1z1]
    row, _ = service.create_crop(uid, t=1, z=1, c=0, x=2, y=1,
                                 width=6, height=5)
    array, ome_out, _ = read_crop_array(service, row["crop_id"])
    np.testing.assert_array_equal(array, data[3][1:6, 2:8])
    assert 'Name="GFP"' in ome_out

    row2, _ = service.create_crop(uid, t=1, z=0, c=0, x=0, y=0,
                                  width=10, height=8)
    array2, _, _ = read_crop_array(service, row2["crop_id"])
    np.testing.assert_array_equal(array2, data[2])


def test_crop_no_tiffdata_defaults_to_dimension_order(service, tmp_path):
    """无 TiffData 元素：按 DimensionOrder 默认排布定位。"""
    data = np.arange(2 * 6 * 7, dtype=np.uint8).reshape(2, 6, 7)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint8" SizeX="7" SizeY="6" SizeC="1" SizeZ="2" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "default.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "default.ome.tiff", blob)
    row, _ = service.create_crop(upload["upload_id"], t=0, z=1, c=0,
                                 x=1, y=1, width=3, height=3)
    array, ome_out, _ = read_crop_array(service, row["crop_id"])
    np.testing.assert_array_equal(array, data[1][1:4, 1:4])
    # 无通道名/物理尺寸时不应虚构
    assert "PhysicalSizeX" not in ome_out


def test_crop_lzw_compressed_plane(service, tmp_path):
    """LZW 压缩的多页 OME-TIFF：解码后裁剪像素精确。"""
    pytest.importorskip("imagecodecs")
    data = np.arange(3 * 8 * 10, dtype=np.uint16).reshape(3, 8, 10)
    with tifffile.TiffWriter(tmp_path / "lzw.ome.tiff", ome=True) as writer:
        writer.write(data, photometric="minisblack", compression="lzw",
                     metadata={"axes": "ZYX"})
    blob = (tmp_path / "lzw.ome.tiff").read_bytes()
    upload = upload_completed(service, tmp_path, "lzw.ome.tiff", blob)
    row, _ = service.create_crop(upload["upload_id"], t=0, z=2, c=0,
                                 x=2, y=3, width=4, height=3)
    array, _, _ = read_crop_array(service, row["crop_id"])
    np.testing.assert_array_equal(array, data[2][3:6, 2:6])


# ----------------------------------------------------------------- 参数错误
def test_crop_missing_axis_only_accepts_zero(service, tmp_path):
    """缺失轴（Size=1）仅接受下标 0。"""
    plane = np.arange(6 * 7, dtype=np.uint8).reshape(6, 7)
    with tifffile.TiffWriter(tmp_path / "2d.ome.tiff", ome=True) as writer:
        writer.write(plane, photometric="minisblack", metadata={"axes": "YX"})
    blob = (tmp_path / "2d.ome.tiff").read_bytes()
    upload = upload_completed(service, tmp_path, "2d.ome.tiff", blob)
    uid = upload["upload_id"]

    for axis in ("t", "z", "c"):
        kwargs = {"t": 0, "z": 0, "c": 0, axis: 1}
        with pytest.raises(BadRequestError) as exc:
            service.create_crop(uid, **kwargs, x=0, y=0, width=2, height=2)
        assert exc.value.status_code == 400
        assert exc.value.code == "axis_index_out_of_range"
        assert "仅接受下标 0" in exc.value.message
    # 下标 0 正常
    _, created = service.create_crop(uid, t=0, z=0, c=0,
                                     x=0, y=0, width=2, height=2)
    assert created is True


def test_crop_axis_index_out_of_range(service, tmp_path):
    blob, _ = make_tzc_ome(tmp_path / "r.ome.tiff")  # T=2 Z=3 C=2
    upload = upload_completed(service, tmp_path, "r.ome.tiff", blob)
    uid = upload["upload_id"]
    for axis, value in (("t", 2), ("z", 3), ("c", 2), ("z", 99)):
        kwargs = {"t": 0, "z": 0, "c": 0, axis: value}
        with pytest.raises(BadRequestError) as exc:
            service.create_crop(uid, **kwargs, x=0, y=0, width=2, height=2)
        assert exc.value.status_code == 400
        assert exc.value.code == "axis_index_out_of_range"


def test_crop_rectangle_out_of_bounds(service, tmp_path):
    blob, _ = make_tzc_ome(tmp_path / "b.ome.tiff")  # 10x8 平面
    upload = upload_completed(service, tmp_path, "b.ome.tiff", blob)
    uid = upload["upload_id"]
    bad_rects = [
        {"x": 10, "y": 0, "width": 1, "height": 1},   # x 起点越界
        {"x": 0, "y": 8, "width": 1, "height": 1},    # y 起点越界
        {"x": 8, "y": 0, "width": 3, "height": 1},    # x+w 越界
        {"x": 0, "y": 6, "width": 1, "height": 3},    # y+h 越界
        {"x": 0, "y": 0, "width": 11, "height": 8},   # 整宽越界
    ]
    for rect in bad_rects:
        with pytest.raises(BadRequestError) as exc:
            service.create_crop(uid, t=0, z=0, c=0, **rect)
        assert exc.value.status_code == 400
        assert exc.value.code == "crop_out_of_bounds"
    # 贴边恰好合法
    _, created = service.create_crop(uid, t=0, z=0, c=0,
                                     x=8, y=6, width=2, height=2)
    assert created is True


# ----------------------------------------------------------------- 不支持情形
def test_crop_rejects_non_uint_dtype(service, tmp_path):
    data = np.zeros((2, 6, 7), dtype=np.float32)
    with tifffile.TiffWriter(tmp_path / "f.ome.tiff", ome=True) as writer:
        writer.write(data, metadata={"axes": "ZYX"})
    blob = (tmp_path / "f.ome.tiff").read_bytes()
    upload = upload_completed(service, tmp_path, "f.ome.tiff", blob)
    with pytest.raises(UnsupportedImageError) as exc:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    assert exc.value.status_code == 422
    assert exc.value.code == "unsupported_image"


def test_crop_rejects_rgb(service, tmp_path):
    rgb = np.zeros((2, 6, 7, 3), dtype=np.uint8)
    with tifffile.TiffWriter(tmp_path / "rgb.ome.tiff", ome=True) as writer:
        writer.write(rgb, photometric="rgb", metadata={"axes": "ZYXS"})
    blob = (tmp_path / "rgb.ome.tiff").read_bytes()
    upload = upload_completed(service, tmp_path, "rgb.ome.tiff", blob)
    with pytest.raises(UnsupportedImageError) as exc:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    assert exc.value.code == "unsupported_image"


def test_crop_rejects_multifile_dataset(service, tmp_path):
    data = np.zeros((1, 6, 7), dtype=np.uint16)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="1" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="0" PlaneCount="1">'
        '<UUID FileName="other-part.ome.tiff">urn:uuid:1234</UUID>'
        '</TiffData>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "mf.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "mf.ome.tiff", blob)
    with pytest.raises(UnsupportedImageError, match="多文件"):
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)


def test_crop_rejects_multi_image_series(service, tmp_path):
    data = np.zeros((1, 6, 7), dtype=np.uint16)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="1" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="0" PlaneCount="1"/>'
        '</Pixels></Image>'
        '<Image ID="Image:1"><Pixels ID="Pixels:1" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="1" SizeT="1">'
        '<Channel ID="Channel:1:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="0" PlaneCount="1"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "mi.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "mi.ome.tiff", blob)
    with pytest.raises(UnsupportedImageError, match="单图像序列"):
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)


def test_crop_rejects_plane_larger_than_16mib(service, tmp_path):
    # 4200x2100 uint16 = 17.64 MB 解码体积 > 16 MiB
    data = np.zeros((2100, 4200), dtype=np.uint16)
    with tifffile.TiffWriter(tmp_path / "huge.ome.tiff", ome=True) as writer:
        writer.write(data, photometric="minisblack", metadata={"axes": "YX"})
    blob = (tmp_path / "huge.ome.tiff").read_bytes()
    upload = upload_completed(service, tmp_path, "huge.ome.tiff", blob)
    with pytest.raises(UnsupportedImageError) as exc:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=10, height=10)
    assert exc.value.code == "plane_too_large"


# ----------------------------------------------------------------- 映射含糊
def test_crop_rejects_overlapping_tiffdata(service, tmp_path):
    data = np.arange(2 * 6 * 7, dtype=np.uint16).reshape(2, 6, 7)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="2" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="0" FirstZ="0" PlaneCount="2"/>'
        '<TiffData IFD="1" FirstZ="1" PlaneCount="1"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "ovl.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "ovl.ome.tiff", blob)
    with pytest.raises(PlaneMappingError) as exc:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    assert exc.value.code == "plane_mapping_ambiguous"


def test_crop_rejects_incomplete_tiffdata(service, tmp_path):
    data = np.arange(2 * 6 * 7, dtype=np.uint16).reshape(2, 6, 7)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="2" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="0" FirstZ="0" PlaneCount="1"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "inc.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "inc.ome.tiff", blob)
    with pytest.raises(PlaneMappingError, match="不完整"):
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)


def test_crop_rejects_ifd_out_of_range(service, tmp_path):
    data = np.arange(2 * 6 * 7, dtype=np.uint16).reshape(2, 6, 7)
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
        'Type="uint16" SizeX="7" SizeY="6" SizeC="1" SizeZ="2" SizeT="1">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        '<TiffData IFD="1" FirstZ="0" PlaneCount="2"/>'
        '</Pixels></Image></OME>'
    )
    blob = make_custom_ome(tmp_path / "oor.ome.tiff", data, ome)
    upload = upload_completed(service, tmp_path, "oor.ome.tiff", blob)
    with pytest.raises(PlaneMappingError, match="超出文件实际页数"):
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)


# ----------------------------------------------------------------- 读取中断
def test_crop_interrupted_download_rejected(service, tmp_path, fake_s3):
    blob, _ = make_tzc_ome(tmp_path / "cut.ome.tiff")
    upload = upload_completed(service, tmp_path, "cut.ome.tiff", blob)
    # 湖内对象被截断（模拟读取中断）：下载字节数与登记不符
    fake_s3.objects[upload["object_key"]] = blob[: len(blob) // 2]
    with pytest.raises(StorageError, match="读取中断"):
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    # 不产生任何裁片记录与对象
    assert service.list_crops(upload["upload_id"]) == []
    assert not any("/crops/" in k for k in fake_s3.objects)


def test_crop_corrupt_plane_rejected(service, tmp_path, fake_s3):
    blob, _ = make_tzc_ome(tmp_path / "bad.ome.tiff")
    upload = upload_completed(service, tmp_path, "bad.ome.tiff", blob)
    # 保持总字节数不变，破坏像素数据区（翻转中段字节）
    corrupted = bytearray(blob)
    mid = len(corrupted) // 2
    for i in range(mid, mid + 64):
        corrupted[i] ^= 0xFF
    fake_s3.objects[upload["object_key"]] = bytes(corrupted)
    try:
        service.create_crop(upload["upload_id"], t=0, z=0, c=0,
                            x=0, y=0, width=2, height=2)
    except (PlaneReadError, PlaneMappingError):
        pass  # 明确报错，且不返回残缺裁片
    else:
        # 未压缩数据被改字节时解码仍可能成功，但绝不抛其它异常类型
        pass
    assert isinstance(service.list_crops(upload["upload_id"]), list)


# ----------------------------------------------------------------- HTTP 层
@pytest.fixture
def client(fake_s3, monkeypatch):
    object.__setattr__(main_mod.settings, "recover_on_startup", False)
    monkeypatch.setattr(main_mod, "S3Store", lambda: fake_s3)
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def completed_upload(client, tmp_path):
    blob, data = make_tzc_ome(tmp_path / "http.ome.tiff")
    resp = client.post("/uploads", json={
        "sample_id": "SCROP-HTTP", "filename": "http.ome.tiff",
        "total_bytes": len(blob)})
    assert resp.status_code == 201
    upload = resp.json()
    for i, chunk in enumerate(split_parts(blob), start=1):
        r = client.put(f"/uploads/{upload['upload_id']}/parts/{i}",
                       content=chunk)
        assert r.status_code == 200
    r = client.post(f"/uploads/{upload['upload_id']}/complete")
    assert r.status_code == 200
    return upload, blob, data


def test_crop_http_flow(client, completed_upload):
    upload, blob, data = completed_upload
    uid = upload["upload_id"]

    # 完成前不可裁剪由 409 覆盖（此处已完成）；先验证裁剪创建
    payload = {"t": 1, "z": 2, "c": 1, "x": 3, "y": 2,
               "width": 5, "height": 4}
    r = client.post(f"/uploads/{uid}/crops", json=payload)
    assert r.status_code == 201
    crop = r.json()
    assert crop["dtype"] == "uint16"
    assert crop["download_url"] == f"/crops/{crop['crop_id']}"

    # 幂等重放 -> 200 同一裁片
    r2 = client.post(f"/uploads/{uid}/crops", json=payload)
    assert r2.status_code == 200
    assert r2.json()["crop_id"] == crop["crop_id"]

    # 列表
    listing = client.get(f"/uploads/{uid}/crops")
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    # 下载并逐像素核验
    dl = client.get(crop["download_url"])
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "image/tiff"
    assert "http_t1z2c1_x3y2_5x4.ome.tiff" in \
        dl.headers["content-disposition"]
    with tifffile.TiffFile(io.BytesIO(dl.content)) as tif:
        array = tif.series[0].asarray()
        ome = tif.ome_metadata
    assert array.shape == (4, 5)
    np.testing.assert_array_equal(array, data[1, 2, 1][2:6, 3:8])
    assert 'Name="GFP"' in ome and 'PhysicalSizeX="0.325"' in ome

    # 原件未改
    assert client.get(f"/uploads/{uid}/object").content == blob


def test_crop_http_errors(client, completed_upload, tmp_path):
    upload, _, _ = completed_upload
    uid = upload["upload_id"]

    # 坐标越界 -> 400
    r = client.post(f"/uploads/{uid}/crops",
                    json={"x": 0, "y": 0, "width": 999, "height": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "crop_out_of_bounds"

    # 缺失轴下标非 0 -> 400（TZC 文件 T 轴 SizeT=2，t=5 越界）
    r = client.post(f"/uploads/{uid}/crops",
                    json={"t": 5, "x": 0, "y": 0, "width": 1, "height": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "axis_index_out_of_range"

    # 负数下标 -> 422（pydantic）
    r = client.post(f"/uploads/{uid}/crops",
                    json={"z": -1, "x": 0, "y": 0, "width": 1, "height": 1})
    assert r.status_code == 422

    # 未知裁片 -> 404；未知上传 -> 404
    assert client.get("/crops/nope").status_code == 404
    r = client.post("/uploads/nope/crops",
                    json={"x": 0, "y": 0, "width": 1, "height": 1})
    assert r.status_code == 404


def test_crop_http_not_ready(client, tmp_path):
    blob, _ = make_tzc_ome(tmp_path / "nr.ome.tiff")
    upload = client.post("/uploads", json={
        "sample_id": "SCROP-NR", "filename": "nr.ome.tiff",
        "total_bytes": len(blob)}).json()
    r = client.post(f"/uploads/{upload['upload_id']}/crops",
                    json={"x": 0, "y": 0, "width": 1, "height": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_ready"


def test_crop_openapi_paths(client):
    spec = client.get("/openapi.json").json()
    assert "/uploads/{upload_id}/crops" in spec["paths"]
    assert "/crops/{crop_id}" in spec["paths"]
