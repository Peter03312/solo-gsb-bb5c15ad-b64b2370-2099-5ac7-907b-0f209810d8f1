#!/usr/bin/env python3
"""调用示例：仅使用标准库（urllib），无需安装第三方依赖。

用法：
    python examples/call_api.py [BASE_URL]

默认 BASE_URL=http://127.0.0.1:8000

演示场景（同一条产线 L1）：
  1. 登记原料批号（含/不含过敏原）
  2. B1 使用含 乳+蛋 原料且完整声明        -> 放行，产线残留 乳、蛋
  3. B2 清洁只核验清除 乳，使用无敏原料    -> 蛋残留且未声明 -> 扣留
  4. 更正 B2 的清洁核验，追加清除 蛋       -> B2 及后续重算 -> 放行
  5. 查询 B2 的历史裁决版本（2 个版本）
  6. 重复批次号 / 未知原料 / 无效过敏原 / 更正目标不存在 的拒绝示例
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def request(method: str, base: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    # 1. 原料登记（过敏原可传代码 MILK/EGG，也可传中文 乳/蛋）
    for lot, name, allergens in [
        ("RM-MILK-01", "蛋奶液", ["乳", "蛋"]),
        ("RM-WATER-01", "纯净水", []),
    ]:
        status, payload = request(
            "POST", base, "/materials",
            {"lot_no": lot, "material_name": name, "allergens": allergens},
        )
        show(f"登记原料 {lot}", status, payload)

    # 2. B1：完整声明 -> 放行
    status, b1 = request(
        "POST", base, "/batches",
        {
            "batch_no": "DEMO-B1",
            "line_code": "L1",
            "material_lots": ["RM-MILK-01"],
            "declared_allergens": ["MILK", "EGG"],
            "cleaning": [],
        },
    )
    show("登记批次 DEMO-B1", status, b1)

    # 3. B2：只清洁 MILK，EGG 残留且未声明 -> 扣留
    status, b2 = request(
        "POST", base, "/batches",
        {
            "batch_no": "DEMO-B2",
            "line_code": "L1",
            "material_lots": ["RM-WATER-01"],
            "declared_allergens": [],
            "cleaning": ["MILK"],
        },
    )
    show("登记批次 DEMO-B2（清洁仅覆盖 MILK）", status, b2)

    # 4. 更正 B2：清洁核验同时清除 EGG -> 重算并放行
    status, corrected = request(
        "PUT", base, f"/batches/{b2['id']}",
        {"cleaning": ["MILK", "EGG"]},
    )
    show("更正 DEMO-B2 清洁核验（追加 EGG）", status, corrected)

    # 5. 历史裁决版本
    status, history = request("GET", base, f"/batches/{b2['id']}/history")
    show("DEMO-B2 历史裁决版本", status, history)

    # 5b. 原料批号主数据更正：纯净水补登为含 SOY -> 所有使用它的批次跨产线重算
    status, mat_corrected = request(
        "PUT", base, "/materials/RM-WATER-01",
        {"allergens": ["SOY"]},
    )
    show("更正原料批号 RM-WATER-01（补登 SOY）", status, mat_corrected)

    # 原料更正审计
    status, mat_audits = request("GET", base, "/materials/RM-WATER-01/corrections")
    show("RM-WATER-01 更正审计", status, mat_audits)

    # 6. 拒绝场景
    status, payload = request(
        "POST", base, "/batches",
        {"batch_no": "DEMO-B1", "material_lots": ["RM-WATER-01"],
         "declared_allergens": [], "cleaning": []},
    )
    show("拒绝：重复批次号", status, payload)

    status, payload = request(
        "POST", base, "/batches",
        {"batch_no": "DEMO-BX", "material_lots": ["NOT-EXIST"],
         "declared_allergens": [], "cleaning": []},
    )
    show("拒绝：未知原料批号", status, payload)

    status, payload = request(
        "POST", base, "/materials",
        {"lot_no": "RM-BAD", "material_name": "X", "allergens": ["星际尘埃"]},
    )
    show("拒绝：无效过敏原标识", status, payload)

    status, payload = request("PUT", base, "/batches/999999", {"cleaning": ["MILK"]})
    show("拒绝：更正目标不存在（批次）", status, payload)

    status, payload = request("PUT", base, "/materials/NO-SUCH-LOT", {"allergens": ["MILK"]})
    show("拒绝：更正目标不存在（原料批号）", status, payload)

    # 产线状态
    status, line = request("GET", base, "/lines/L1")
    show("产线 L1 当前状态", status, line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
