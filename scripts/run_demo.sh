#!/usr/bin/env bash
# 一键启动环境并运行全部故障演示。
# 用法: ./scripts/run_demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo ">> 构建并启动 MinIO + API（持久化卷）"
docker compose up -d --build

echo ">> 等待 API 健康检查通过"
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://localhost:8000/health && echo

echo ">> 在 API 容器内生成无隐私合成样例 OME-TIFF"
docker compose exec -T api python scripts/generate_samples.py /sample-data

echo ">> 运行端到端故障演示"
docker compose exec -T \
  -e API_BASE=http://127.0.0.1:8000 \
  -e S3_ENDPOINT=http://minio:9000 \
  -e SAMPLE_DIR=/sample-data \
  api python scripts/demo.py

cat <<'TIP'

------------------------------------------------------------
演示完成。可用入口：
  Swagger 文档 : http://localhost:8000/docs
  MinIO 控制台 : http://localhost:9001  (minioadmin/minioadmin)
停止环境     : docker compose down
连数据一起清除: docker compose down -v
------------------------------------------------------------
TIP
