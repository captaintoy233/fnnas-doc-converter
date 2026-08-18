# DocConverter Docker 镜像
# 批量文档转换 → Markdown → WeKnora 知识库
FROM hub.1panel.dev/library/python:3.11-slim-bookworm

WORKDIR /app

# 安装系统依赖:
# - libgomp1: PyMuPDF 需要
# - p7zip-full: CHM/rar/7z 解压（转换器依赖 7zz/7z）
# - tesseract-ocr + chi_sim: 图片型 PDF OCR（pdf.ocr_enabled=true 时启用）
# - fonts-noto-cjk: PDF 渲染中文
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    libgomp1 \
    p7zip-full \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务代码和配置
COPY converters/ /app/converters/
COPY main.py cli.py config.py scanner.py batch.py watcher.py \
     weknora_client.py sniffer.py registry.py paths.py extract.py /app/
COPY converter.yaml /app/config/converter.yaml

# 创建数据目录
RUN mkdir -p /data/uploads /data/output /data/input /data/tmp /data/config \
    && chown -R nobody:nogroup /data /app

# 默认配置链接
RUN ln -sf /app/config/converter.yaml /data/config/converter.yaml

# 非 root 运行（安全）
USER nobody

EXPOSE 8080

VOLUME ["/data/input", "/data/output", "/data/uploads"]

CMD ["python3", "-u", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
