"""测试夹具：每个用例使用独立临时 SQLite 库，直接调用服务层。"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("ALLERGEN_DB_PATH", path)

    # 延迟导入，使 config.DB_PATH 读取到临时路径
    from app import config, database

    monkeypatch.setattr(config, "DB_PATH", path)
    database.init_db(path)
    yield path

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture()
def svc(temp_db):
    from app import services

    return services


def make_material(svc, lot_no: str, allergens: list[str], name: str = "原料"):
    return svc.create_material(
        {"lot_no": lot_no, "material_name": name, "allergens": allergens}
    )
