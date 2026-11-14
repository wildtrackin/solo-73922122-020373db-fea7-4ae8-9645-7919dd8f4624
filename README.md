# OME-TIFF 断点续传入湖 API

单进程服务，把网络易中断的大幅 OME-TIFF 以 **8 MiB 分片、真实 S3 multipart upload**
续传进本地 MinIO 数据湖；合并后核验 OME 元数据与 TIFF 页目录，**坏图不得入湖**；
分析程序只能读到完整原件。

入湖后还可按 **T/Z/C 逻辑下标 + X/Y 像素矩形** 裁剪焦层局部视野：
严格按 OME 轴与 TiffData 映射定位目标平面，解码后生成可下载的
**二维 OME-TIFF**（保留通道名与像素物理尺寸，原件不改），直接交给
只接收二维图像的分析程序。

技术栈：FastAPI · SQLite（WAL）· tifffile · boto3（S3 multipart）· 本地 MinIO · Uvicorn

---

## 1. 快速开始

```bash
# 启动持久化 MinIO + API（含故障注入开关）
./scripts/run_demo.sh
```

或手动分步：

```bash
docker compose up -d --build
# 生成无隐私的合成 OME-TIFF 样例（约 24 MiB 大图 / 小图 / 坏图）
docker compose exec api python scripts/generate_samples.py /sample-data
# 运行 7 个端到端故障场景
docker compose exec api python scripts/demo.py
```

入口：

| 入口 | 地址 | 凭据 |
| --- | --- | --- |
| Swagger UI（可操作全流程） | http://localhost:8000/docs | — |
| OpenAPI Schema | http://localhost:8000/openapi.json | — |
| MinIO S3 API | http://localhost:9000 | minioadmin / minioadmin |
| MinIO 控制台 | http://localhost:9001 | minioadmin / minioadmin |

停止与清盘：

```bash
docker compose down        # 停止，保留数据卷（续传/恢复状态仍在）
docker compose down -v     # 连同 MinIO 与 SQLite 持久化卷一起清除
```

---

## 2. API 全流程

### 2.1 创建上传（按样本号，声明总字节数）

```bash
curl -X POST http://localhost:8000/uploads \
  -H 'Content-Type: application/json' \
  -d '{"sample_id":"S-2026-001","filename":"slide.ome.tiff","total_bytes":25169920}'
```

响应给出分片规划（`part_size` 固定 8 MiB，末片按总字节取余可短）：

```json
{
  "upload_id": "ab12...", "object_key": "samples/S-2026-001/ab12.../slide.ome.tiff",
  "total_bytes": 25169920, "part_size": 8388608,
  "expected_parts": 4, "status": "open"
}
```

### 2.2 查询已接纳片号（断点续传决策）

```bash
curl http://localhost:8000/uploads/$UID/parts
# -> received_parts / missing_parts / bytes_received
```

### 2.3 上传 / 重传分片（请求体即原始分片字节）

```bash
curl -X PUT --data-binary @part-003.bin \
  http://localhost:8000/uploads/$UID/parts/3
```

- 非末片必须恰好 **8 MiB**，末片按声明大小取余；
- **同片同字节重传 → 200 幂等**（直接返回，不再次请求 S3）；
- **同片异内容 → 409**，已接纳分片绝不被覆盖；
- 不同片号可并发上传，服务按 `upload_id` 串行化同一任务。

### 2.4 完成（齐全后才能合并）

```bash
curl -X POST http://localhost:8000/uploads/$UID/complete
```

分片缺号、字节合计与声明不符都会返回 409。合并成功后自动：

1. `head_object` 核对合并对象大小 == 声明总字节；
2. 下载到本地临时文件，用 tifffile 逐页遍历 **TIFF 页目录（IFD）**，
   校验 **OME-XML 元数据**、维度与 dtype；
3. 若创建时给了 `content_sha256`，再做整图哈希校验；
4. 全部通过才置 `completed` 并开放查询；任何失败 → 删除远端对象、置 `invalid`，坏图不入湖。

成功响应直接带回提取的尺寸：

```json
{"status":"completed","width":2048,"height":2048,"pages":3,"dtype":"uint16","axes":"ZYX"}
```

### 2.5 放弃（与完成竞争，只有一种生效）

```bash
curl -X POST http://localhost:8000/uploads/$UID/abort
```

- 通过 SQLite CAS 状态流转保证与 `complete` 竞争时只有一种生效；
- 先落库 `aborted`（此后迟到分片在入口即被 **410** 拒绝），
  再 `AbortMultipartUpload` **清除远端全部分片**，不留残件。

### 2.6 仅完成任务开放的查询

```bash
curl http://localhost:8000/uploads/$UID/dimensions   # 尺寸/页数/dtype/axes
curl -O http://localhost:8000/uploads/$UID/object    # 流式下载完整原件
```

`open / completing / aborted / invalid` 状态访问均返回 409/410，分析程序无法读到半成品。

### 2.7 焦层视野裁剪（仅 completed）

```bash
# 取 T=1、Z=2、C=1 平面上 (x=40, y=24) 起 200x120 的局部视野
curl -X POST http://localhost:8000/uploads/$UID/crops \
  -H 'Content-Type: application/json' \
  -d '{"t":1,"z":2,"c":1,"x":40,"y":24,"width":200,"height":120}'
```

首次创建返回 **201**，响应含 `download_url`；同一 upload_id + 同一组参数
幂等，重放返回 **200** 与同一 `crop_id`，不重复解码/发布：

```json
{
  "crop_id": "f31c...", "upload_id": "ab12...",
  "t": 1, "z": 2, "c": 1, "x": 40, "y": 24, "width": 200, "height": 120,
  "dtype": "uint16", "size_bytes": 48720, "sha256": "…",
  "download_url": "/crops/f31c..."
}
```

```bash
curl http://localhost:8000/uploads/$UID/crops   # 列出该上传的全部裁片
curl -OJ http://localhost:8000/crops/$CROP_ID   # 下载二维 OME-TIFF（image/tiff）
```

裁剪语义与约束：

- 按 **OME 轴及 TiffData 映射**（`FirstZ/FirstC/FirstT/IFD/PlaneCount` +
  `DimensionOrder`）定位目标平面；映射重叠、越界或不完整一律
  **422 `plane_mapping_ambiguous`**，绝不猜测；
- 裁片为**单平面二维 OME-TIFF**，宽高与像素逐点对应裁剪区域；
  原图有**通道名称、像素物理尺寸**（PhysicalSizeX/Y 及单位）时一并保留；
  **原件只读不改**；
- 仅支持：**单文件**数据集（TiffData 不引用外部文件）、**单图像序列**、
  **灰度**（SamplesPerPixel=1）**uint8/uint16**，且目标平面解码后
  **不超过 16 MiB**；超出即 422 `unsupported_image` / `plane_too_large`；
- SizeZ/SizeC/SizeT 为 1 的**缺失轴仅接受下标 0**，其余下标越界
  → 400 `axis_index_out_of_range`；裁剪矩形越出图像
  → 400 `crop_out_of_bounds`；
- 湖内对象读取中断（下载字节数不符）→ 502 `storage_error`；
  目标平面解码失败 → 500 `plane_read_failed`；任何失败都**不返回残缺裁片**，
  也不会留下裁片记录或对象。

---

## 3. 状态机与崩溃恢复

```
                 PUT parts(幂等/409)
   open ┌──────────────────────────────┐
    │   │                              │
    │   ▼                              │
    │  (CAS open→completing)           │ POST abort (CAS open/completing→aborted)
    │ completing                       │
    │   │ 合并 S3 → 下载 → tifffile 核验
    │   ├─ 通过 → completed（开放原件/尺寸）
    │   └─ 失败 → invalid（删远端对象，不开放）
    └──→ aborted（清远端分片，拒迟到写入）
```

启动时 `recover_on_startup()` 对每个 `open / completing` 任务核实并续完：

| 崩溃窗口 | 重启后的行为 |
| --- | --- |
| 分片已写 S3、SQLite 未落库 | 与 S3 `ListParts` 对账；本地无哈希的片不冒充已接纳，按缺片返回，客户端**只补缺片**，不重传整图 |
| `CompleteMultipartUpload` 已成功、回执丢失/进程退出 | `HeadObject` 发现对象已存在且大小正确 → **跳过重复合并**，直接下载核验续完，不重复发布 |
| 核验阶段崩溃 | 重新下载核验；坏图删对象置 `invalid` |
| 无数据库归属的远端 multipart | 启动时作为孤儿上传 `AbortMultipartUpload` 清除 |
| 客户端重试 `complete` | `completed` 直接幂等返回；`completing` 走恢复式收尾，永久卡不住 |

任务在 SQLite 与 MinIO 数据卷中持久化，`docker compose down` 后再 `up` 仍可续传/恢复。

---

## 4. 故障演示（8 个场景）

`ENABLE_FAULTS=true` 时开放 `POST /dev/faults` 注入两个真实崩溃点
（SIGTERM，容器 `restart: unless-stopped` 自动拉起，等价主机重启）：

| 场景 | 验证点 |
| --- | --- |
| 1 小图全流程 | 合并核验后尺寸/原件才开放 |
| 2 大图中断续传 | 4 分片（末片 1199 字节），按 `missing_parts` 续传，原件逐字节一致 |
| 3 幂等与冲突 | 同片重传 200、异内容 409 且不污染 |
| 4 放弃 | 远端分片清除、迟到写入 410、无残件 |
| 5 `crash_after_part` | 第 3 片写 S3 后崩溃，重启只补缺片即完成 |
| 6 `crash_after_merge` | 合并后崩溃，重启续完；重试 complete 幂等、湖中仅一个对象 |
| 7 坏图 | 缺 OME-XML 的 TIFF 返回 422、对象删除、查询不开放 |
| 8 焦层裁剪 | TZC 多维图按 T/Z/C+矩形裁剪，裁片像素逐点核验、通道名/物理尺寸保留、原件不变、越界 400 |

```bash
docker compose exec api python scripts/demo.py
```

生产环境设置 `ENABLE_FAULTS=false`，故障端点返回 403。

无 Docker 的环境可用内存 S3 在真实 uvicorn HTTP 上跑通同一套演示脚本：

```bash
python scripts/smoke_local.py
```

---

## 5. 测试

```bash
python -m pytest          # 52 项：服务层并发/竞争/恢复 + HTTP 层全语义 + 裁剪全场景
```

覆盖：多分片并发、同片幂等/409、片长校验、完成/放弃 CAS 竞争（多轮）、
放弃清残件与迟到写入拒绝、无 OME 元数据/截断 TIFF/SHA256 不符三类坏图拦截、
两类崩溃窗口恢复、孤儿 multipart 清理、complete 幂等；
裁剪的像素级核验（TZC 定位、多块 TiffData 映射、DimensionOrder 默认排布）、
通道名/物理尺寸保留、缺轴下标/矩形越界/轴越界 400、非 uint8-16/RGB/多文件/
多序列/平面超 16 MiB 422、TiffData 重叠/不完整/IFD 越界 422、
读取中断 502、同参幂等与裁片下载。

## 6. 目录结构

```
app/
  config.py            # 环境变量配置、8MiB 分片/16MiB 平面常量
  db.py                # SQLite WAL schema、CAS 状态流转、裁片幂等落库
  storage.py           # boto3 真实 S3 multipart 适配
  image_validation.py  # OME-XML + IFD 页目录核验、尺寸提取
  crop.py              # OME 轴/TiffData 平面定位、灰度 uint8/16 裁剪、二维 OME-TIFF 生成
  faults.py            # 故障注入（SIGTERM）
  service.py           # 生命周期/幂等/并发/放弃/启动恢复/裁剪编排
  main.py              # FastAPI 路由与错误映射
scripts/
  generate_samples.py  # 无隐私合成 OME-TIFF（含 TZC 多维样例）
  demo.py              # Docker 内 8 场景故障演示
  smoke_local.py       # 无 Docker 本地 HTTP 冒烟
tests/                 # pytest 套件（内存 S3 模拟 multipart 语义）
docker-compose.yml     # 持久化 MinIO + API
```

## 7. 关键设计约束

- 单进程 + 每 `upload_id` 一把 `RLock`：同任务分片/完成/放弃串行，不同任务不互斥；
- 网络 IO（S3 调用）不持数据库事务，SQLite 事务只覆盖本地状态变更；
- 所有“终态发布”以 SQLite 状态为准，S3 对象存在但状态未完成时一律不可读；
- 对象键含 `upload_id`，同一样本反复上传不会互相覆盖；
- 裁片以 `(upload_id, t, z, c, x, y, width, height)` 唯一约束幂等发布，
  并发同参裁剪只有一个入湖，其余清理自身对象后返回已存在裁片；
- 裁剪只读原件（下载到临时文件后解码），裁片另写新对象，原件逐字节不变。
