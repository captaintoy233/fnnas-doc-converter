#!/bin/bash
# DocConverter v2.0 构建脚本
# 在飞牛OS或任意有Docker的机器上运行

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 DocConverter v2.0 Docker 构建"
echo "================================"

# 1. 构建转换器镜像
echo ""
echo "📦 [1/4] 构建转换器镜像..."
docker build -t docconverter:2.0.0 -f app/server/Dockerfile app/server/

# 2. 创建数据目录
echo ""
echo "📁 [2/4] 创建数据目录..."
mkdir -p /data/input /data/output /data/uploads

# 3. 启动容器
echo ""
echo "🔍 [3/4] 启动容器..."
docker rm -f doc-converter 2>/dev/null || true
docker run -d --name doc-converter -p 8080:8080 \
  -v /data/input:/data/input \
  -v /data/output:/data/output \
  -v /data/uploads:/data/uploads \
  -e WEKNORA_ENABLED=false \
  -e CONVERTER_SOURCE_DIR=/data/input \
  -e CONVERTER_OUTPUT_DIR=/data/output \
  docconverter:2.0.0

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

# 4. 验证
echo ""
echo "🧪 [4/4] 验证..."
echo "   健康检查:"
curl -s http://localhost:8080/api/health | python3 -m json.tool
echo ""

# 复制测试样本到源目录
if [ -d "test_samples" ]; then
  cp test_samples/* /data/input/ 2>/dev/null || true
  echo "   测试文件已复制到 /data/input/"
fi

echo ""
echo "✅ 构建完成!"
echo ""
echo "📋 访问地址:"
echo "   Web UI:     http://localhost:8080"
echo "   API 文档:   http://localhost:8080/docs"
echo ""
echo "📋 后续命令:"
echo "   全栈部署 (含WeKnora): docker compose up -d"
echo "   查看日志:              docker logs -f doc-converter"
echo "   放入源文件:            cp <文件> /data/input/"
echo "   查看输出:              ls /data/output/"
