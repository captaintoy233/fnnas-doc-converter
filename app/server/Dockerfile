# DocConverter Docker 镜像
# 纯 Python 转换器层 (~70MB)，不包含 LibreOffice/Tesseract
FROM python:3.11-slim-bookworm

WORKDIR /app

# 安装系统依赖 (仅需 C 编译的 Python 包)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务代码
COPY converters/ /app/converters/
COPY main.py /app/

# 上传目录
RUN mkdir -p /data/uploads

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
