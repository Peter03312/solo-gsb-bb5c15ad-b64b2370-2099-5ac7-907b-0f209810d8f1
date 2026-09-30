"""HTTP API 端到端测试（FastAPI TestClient + 临时 SQLite）。"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("ALLERGEN_DB_PATH", path)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from app import config, database
    from app.main import app

    monkeypatch.setattr(config, "DB_PATH", path)
    database.init_db(path)
    with TestClient(app) as c:
        yield c


def _post_material(client, lot_no, allergens, name="原料"):
    return client.post(
        "/materials",
        json={"lot_no": lot_no, "material_name": name, "allergens": allergens},
    )


def test_health_and_catalog(client):
    assert client.get("/health").json()["status"] == "ok"
    allergens = client.get("/allergens").json()
    codes = {a["code"] for a in allergens}
    assert "MILK" in codes
    assert next(a for a in allergens if a["code"] == "MILK")["name"] == "乳"


def test_full_flow_over_http(client):
    assert _post_material(client, "RM-MILK", ["MILK", "EGG"]).status_code == 201
    assert _post_material(client, "RM-SAFE", []).status_code == 201

    # B1 声明齐全 -> 放行
    r = client.post(
        "/batches",
        json={
            "batch_no": "B1",
            "material_lots": ["RM-MILK"],
            "declared_allergens": ["乳", "蛋"],
            "cleaning": [],
        },
    )
    assert r.status_code == 201
    b1 = r.json()
    assert b1["verdict"]["decision"] == "RELEASE"

    # B2 只清洁 MILK，EGG 残留且未声明 -> 扣留
    r = client.post(
        "/batches",
        json={
            "batch_no": "B2",
            "material_lots": ["RM-SAFE"],
            "declared_allergens": [],
            "cleaning": ["MILK"],
        },
    )
    b2 = r.json()
    assert b2["verdict"]["decision"] == "WITHHELD"
    assert b2["verdict"]["remaining_carryover"] == ["EGG"]

    # 更正 B2 清洁同时清除 EGG -> B2/B3 链路放行
    r = client.put(
        f"/batches/{b2['id']}",
        json={"cleaning": ["MILK", "EGG"]},
    )
    assert r.status_code == 200
    assert r.json()["verdict"]["decision"] == "RELEASE"

    # 历史版本
    hist = client.get(f"/batches/{b2['id']}/history").json()
    assert [v["version"] for v in hist["versions"]] == [1, 2]

    # 产线残留已清空
    line = client.get("/lines/L1").json()
    assert line["residual_allergens"] == []


def test_api_rejects_duplicates_and_unknowns(client):
    _post_material(client, "RM-1", ["MILK"])

    r = client.post(
        "/batches",
        json={"batch_no": "X1", "material_lots": ["RM-UNKNOWN"],
              "declared_allergens": [], "cleaning": []},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_MATERIAL"

    r = _post_material(client, "RM-1", [])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_MATERIAL"

    r = client.post(
        "/batches",
        json={"batch_no": "X!", "material_lots": ["RM-1"],
              "declared_allergens": ["不存在的过敏原"], "cleaning": []},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ALLERGEN"

    assert client.put("/batches/9999", json={"cleaning": []}).status_code == 404


def test_listing_endpoints(client):
    _post_material(client, "RM-1", ["MILK"])
    client.post(
        "/batches",
        json={"batch_no": "B1", "material_lots": ["RM-1"],
              "declared_allergens": ["MILK"], "cleaning": []},
    )
    assert len(client.get("/materials").json()) == 1
    assert len(client.get("/batches").json()) == 1
    assert client.get("/materials/RM-1").status_code == 200


def test_material_correction_over_http(client):
    _post_material(client, "RM-1", ["MILK"])
    client.post(
        "/batches",
        json={"batch_no": "B1", "material_lots": ["RM-1"],
              "declared_allergens": ["MILK"], "cleaning": []},
    )

    # 原料补登 EGG 过敏原 -> B1 立即变为扣留
    r = client.put("/materials/RM-1", json={"allergens": ["MILK", "EGG"]})
    assert r.status_code == 200
    body = r.json()
    assert body["allergens"] == ["EGG", "MILK"]
    assert body["recalculated_batch_ids"] == [1]

    batch = client.get("/batches/1").json()
    assert batch["verdict"]["decision"] == "WITHHELD"
    assert batch["verdict"]["triggered_by"] == "MATERIAL_CORRECTION"

    audits = client.get("/materials/RM-1/corrections").json()
    assert len(audits) == 1
    assert audits[0]["changed_fields"] == ["allergens"]

    assert client.put("/materials/NOPE", json={"allergens": []}).status_code == 404
