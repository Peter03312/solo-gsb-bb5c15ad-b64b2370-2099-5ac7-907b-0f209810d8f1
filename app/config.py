"""运行配置：均可用环境变量覆盖。"""
from __future__ import annotations

import os

# SQLite 数据库文件路径（Docker 中由环境变量覆盖为 /data/allergen.db）
DB_PATH = os.environ.get("ALLERGEN_DB_PATH", os.path.join("data", "allergen.db"))

# 服务监听地址 / 端口
HOST = os.environ.get("ALLERGEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("ALLERGEN_PORT", "8000"))

# 未显式指定产线时使用的默认产线标识
DEFAULT_LINE = os.environ.get("ALLERGEN_DEFAULT_LINE", "L1")
