FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ALLERGEN_DB_PATH=/data/allergen.db \
    ALLERGEN_HOST=0.0.0.0 \
    ALLERGEN_PORT=8000

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据库持久化目录（compose 中挂载命名卷）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动即建库建表（init_db 幂等），随后提供 HTTP 服务
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
