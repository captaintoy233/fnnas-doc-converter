#!/bin/bash
# DocConverter Docker 构建脚本
# 在飞牛OS或任意有Docker的机器上运行

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 DocConverter Docker 构建"
echo "========================"

# 1. 构建转换器镜像
echo ""
echo "📦 [1/3] 构建转换器镜像..."
docker build -t docconverter:1.0.0 -f app/server/Dockerfile app/server/

# 2. 测试容器启动
echo ""
echo "🔍 [2/3] 启动测试容器..."
docker rm -f doc-converter 2>/dev/null || true
docker run -d --name doc-converter -p 8080:8080 \
  -v docconverter_uploads:/data/uploads \
  docconverter:1.0.0

# 等待服务启动
echo "   等待服务就绪..."
sleep 3
for i in $(seq 1 10); do
  if curl -s http://localhost:8080/api/health > /dev/null 2>&1; then
    echo "   ✅ 服务已就绪 (尝试 $i 次)"
    break
  fi
  echo "   等待中... (尝试 $i)"
  sleep 2
done

# 3. 验证转换功能
echo ""
echo "🧪 [3/3] 验证转换功能..."
echo "   支持的格式:"
curl -s http://localhost:8080/api/formats | python3 -m json.tool

# 测试 OFD 转换
echo ""
echo "   转换测试 (OFD):"
curl -s -X POST http://localhost:8080/api/convert \
  -F "file=@test_sample.ofd" 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'   ✅ {d[\"filename\"]}: {d[\"length\"]} chars via {d[\"converter\"]}')" \
  || echo "   ⚠️ 跳过OFD测试 (无测试文件)"

echo ""
echo "✅ 构建完成!"
echo ""
echo "📋 后续命令:"
echo "   docker stop doc-converter     # 停止测试容器"
echo "   docker compose up -d          # 启动全栈 (含WeKnora)"
echo "   docker compose -f app/docker/docker-compose.yaml up -d  # 飞牛编排"
