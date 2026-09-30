"""核心业务逻辑：登记、过敏原裁决、清洁残留传递、更正级联重算。

裁决规则（按产线、按 seq 顺序）：
  incoming      = 上一批的“实际可能含有的过敏原”（上一批即使被扣留也照常传递）
  covered       = 本次清洁核验中，与 incoming 命中的过敏原（只有这些被清除）
  remaining     = incoming - covered
  material_al   = 本批所有原料批号过敏原的并集
  actual        = remaining ∪ material_al          （本批实际可能含有的过敏原）
  undeclared    = actual - declared                 （未声明项）
  undeclared 非空 => WITHHELD（扣留），否则 RELEASE（放行）
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from . import config
from .allergens import AllergenError, display_name, normalize_allergens
from .database import connect, transaction

# 同一进程内对登记/更正的串行化；SQLite 自身写锁为最终保障。
_WRITER_LOCK = threading.Lock()

RELEASE = "RELEASE"
WITHHELD = "WITHHELD"


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------- 工具

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: str) -> Any:
    return json.loads(value) if value else []


def _normalize(values: list[str] | None) -> list[str]:
    try:
        return normalize_allergens(values)
    except AllergenError as exc:
        raise ApiError(400, "INVALID_ALLERGEN", str(exc)) from exc


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _get_or_create_line(conn: sqlite3.Connection, line_code: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO lines(code, created_at) VALUES (?, ?)",
        (line_code, _now()),
    )


def _latest_verdict(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verdict_versions WHERE batch_id=? ORDER BY version DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


# ---------------------------------------------------------------- 裁决与重算

def _build_reason(
    undeclared: list[str],
    sources: dict[str, dict[str, Any]],
    actual: list[str],
) -> str:
    """生成中文裁决原因，逐项说明未声明过敏原的来源。"""
    if undeclared:
        items: list[str] = []
        for code in undeclared:
            src = sources.get(code, {})
            ways: list[str] = []
            if src.get("carryover"):
                ways.append("上批残留且未被本次清洁核验清除")
            lots = src.get("material_lots", [])
            if lots:
                ways.append(f"本批原料批号（{'、'.join(lots)}）带入")
            items.append(f"{display_name(code)}[{code}]：" + "，".join(ways))
        return "扣留——以下实际可能含有的过敏原未在成品声明：" + "；".join(items)

    if actual:
        names = "、".join(f"{display_name(c)}[{c}]" for c in actual)
        return f"放行——实际可能含有的过敏原（{names}）均已声明"
    return "放行——无过敏原残留或带入，亦无未声明项"


def _append_verdict(
    conn: sqlite3.Connection,
    batch: sqlite3.Row,
    incoming: list[str],
    triggered_by: str,
    correction_id: int | None = None,
    material_correction_id: int | None = None,
) -> sqlite3.Row:
    """为单个批次计算并追加一个裁决版本，返回新版本行。"""
    cleaning = _loads(batch["cleaning"])
    declared = _loads(batch["declared_allergens"])

    # 清洁只对“上批残留”中被核验确认的项目生效
    covered = sorted(set(incoming) & set(cleaning))
    remaining = sorted(a for a in incoming if a not in set(cleaning))

    # 原料过敏原并集，并记录每个过敏原来自哪些批号
    lot_rows = conn.execute(
        """
        SELECT ml.lot_no AS lot_no, ml.allergens AS allergens
        FROM batch_materials bm
        JOIN material_lots ml ON ml.lot_no = bm.material_lot
        WHERE bm.batch_id = ?
        ORDER BY bm.position
        """,
        (batch["id"],),
    ).fetchall()

    material_set: set[str] = set()
    sources: dict[str, dict[str, Any]] = {
        code: {"carryover": True, "material_lots": []} for code in remaining
    }
    for row in lot_rows:
        for code in _loads(row["allergens"]):
            material_set.add(code)
            entry = sources.setdefault(code, {"carryover": False, "material_lots": []})
            if row["lot_no"] not in entry["material_lots"]:
                entry["material_lots"].append(row["lot_no"])

    actual = sorted(set(remaining) | material_set)
    undeclared = sorted(code for code in actual if code not in set(declared))
    decision = WITHHELD if undeclared else RELEASE
    reason = _build_reason(undeclared, sources, actual)

    last = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM verdict_versions WHERE batch_id=?",
        (batch["id"],),
    ).fetchone()
    version = last["v"] + 1

    conn.execute(
        """
        INSERT INTO verdict_versions(
            batch_id, version, incoming_residual, cleaning_covered,
            remaining_carryover, material_allergens, actual_allergens,
            undeclared_allergens, sources, decision, reason,
            triggered_by, correction_id, material_correction_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch["id"], version,
            json.dumps(incoming, ensure_ascii=False),
            json.dumps(covered, ensure_ascii=False),
            json.dumps(remaining, ensure_ascii=False),
            json.dumps(sorted(material_set), ensure_ascii=False),
            json.dumps(actual, ensure_ascii=False),
            json.dumps(undeclared, ensure_ascii=False),
            json.dumps(sources, ensure_ascii=False),
            decision, reason, triggered_by, correction_id,
            material_correction_id, _now(),
        ),
    )
    return _latest_verdict(conn, batch["id"])  # type: ignore[return-value]


def _recompute_from(
    conn: sqlite3.Connection,
    line_code: str,
    start_seq: int,
    correction_id: int | None = None,
    material_correction_id: int | None = None,
    directly_affected: set[int] | None = None,
) -> list[int]:
    """从产线上 seq=start_seq 的批次开始顺序重算（含后续所有批次）。

    每个被重算的批次都追加一个新的裁决版本，历史版本永不覆盖。
    directly_affected 中的批次标记为 MATERIAL_CORRECTION（直接使用了被更正原料），
    其余窗口内批次标记为 RECASCADE（级联）。
    """
    directly_affected = directly_affected or set()

    # 重算窗口之前的上一批残留（窗口从第一批发车时为 []）
    incoming: list[str] = []
    if start_seq > 1:
        prior = conn.execute(
            "SELECT id FROM batches WHERE line_code=? AND seq < ? "
            "ORDER BY seq DESC LIMIT 1",
            (line_code, start_seq),
        ).fetchone()
        if prior is not None:
            prior_verdict = _latest_verdict(conn, prior["id"])
            if prior_verdict is not None:
                incoming = _loads(prior_verdict["actual_allergens"])

    batches = conn.execute(
        "SELECT * FROM batches WHERE line_code=? AND seq >= ? ORDER BY seq",
        (line_code, start_seq),
    ).fetchall()

    recalculated: list[int] = []
    for index, batch in enumerate(batches):
        if material_correction_id is not None and batch["id"] in directly_affected:
            triggered_by = "MATERIAL_CORRECTION"
        elif correction_id is not None and index == 0:
            triggered_by = "CORRECTION"
        elif correction_id is not None or material_correction_id is not None:
            triggered_by = "RECASCADE"
        else:
            triggered_by = "REGISTER"
        verdict = _append_verdict(
            conn, batch, incoming, triggered_by,
            correction_id=correction_id,
            material_correction_id=material_correction_id,
        )
        incoming = _loads(verdict["actual_allergens"])
        recalculated.append(batch["id"])
    return recalculated


# ---------------------------------------------------------------- 原料批号

def create_material(payload: dict[str, Any]) -> dict[str, Any]:
    allergens = _normalize(payload.get("allergens"))
    lot_no = payload["lot_no"].strip()
    name = payload["material_name"].strip()
    with _WRITER_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                exists = conn.execute(
                    "SELECT 1 FROM material_lots WHERE lot_no=?", (lot_no,)
                ).fetchone()
                if exists:
                    raise ApiError(
                        409, "DUPLICATE_MATERIAL", f"原料批号已存在: {lot_no}"
                    )
                conn.execute(
                    "INSERT INTO material_lots(lot_no, material_name, allergens, created_at)"
                    " VALUES (?,?,?,?)",
                    (lot_no, name, json.dumps(allergens, ensure_ascii=False), _now()),
                )
        finally:
            conn.close()
    return get_material(lot_no)


def get_material(lot_no: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM material_lots WHERE lot_no=?", (lot_no,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ApiError(404, "MATERIAL_NOT_FOUND", f"原料批号不存在: {lot_no}")
    return _serialize_material(row)


def list_materials() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM material_lots ORDER BY lot_no"
        ).fetchall()
    finally:
        conn.close()
    return [_serialize_material(r) for r in rows]


def _serialize_material(row: sqlite3.Row) -> dict[str, Any]:
    allergens = _loads(row["allergens"])
    return {
        "lot_no": row["lot_no"],
        "material_name": row["material_name"],
        "allergens": allergens,
        "allergen_names": [display_name(c) for c in allergens],
        "created_at": row["created_at"],
    }


def correct_material(lot_no: str, payload: dict[str, Any]) -> dict[str, Any]:
    """更正原料批号主数据（原料名称 / 过敏原）。

    过敏原更正后，找出所有直接使用该批号的批次，按产线从最早受影响批次起
    重算该批及后续批次（可能跨多条产线）；更正点之前的批次不受影响。
    """
    new_allergens: list[str] | None = None
    new_name: str | None = None
    changed: list[str] = []

    if payload.get("allergens") is not None:
        new_allergens = _normalize(payload["allergens"])
        changed.append("allergens")
    if payload.get("material_name") is not None:
        new_name = payload["material_name"].strip()
        if not new_name:
            raise ApiError(400, "EMPTY_FIELD", "material_name 不能为空")
        changed.append("material_name")
    if not changed:
        raise ApiError(
            400,
            "NO_CHANGES",
            "未提供任何可更正字段（material_name / allergens）",
        )

    with _WRITER_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = conn.execute(
                    "SELECT * FROM material_lots WHERE lot_no=?", (lot_no,)
                ).fetchone()
                if row is None:
                    raise ApiError(
                        404, "MATERIAL_NOT_FOUND", f"更正目标不存在: 原料批号 {lot_no}"
                    )

                old_values = {
                    "material_name": row["material_name"],
                    "allergens": _loads(row["allergens"]),
                }
                new_values = {
                    "material_name": new_name if new_name is not None else row["material_name"],
                    "allergens": new_allergens
                    if new_allergens is not None
                    else _loads(row["allergens"]),
                }

                if new_name is not None:
                    conn.execute(
                        "UPDATE material_lots SET material_name=? WHERE lot_no=?",
                        (new_name, lot_no),
                    )
                if new_allergens is not None:
                    conn.execute(
                        "UPDATE material_lots SET allergens=? WHERE lot_no=?",
                        (json.dumps(new_allergens, ensure_ascii=False), lot_no),
                    )

                cur = conn.execute(
                    "INSERT INTO material_correction_events(lot_no, changed_fields,"
                    " old_values, new_values, created_at) VALUES (?,?,?,?,?)",
                    (
                        lot_no,
                        json.dumps(changed, ensure_ascii=False),
                        json.dumps(old_values, ensure_ascii=False),
                        json.dumps(new_values, ensure_ascii=False),
                        _now(),
                    ),
                )
                event_id = cur.lastrowid

                # 所有直接使用该批号的批次
                affected_rows = conn.execute(
                    """
                    SELECT b.id AS id, b.line_code AS line_code, b.seq AS seq
                    FROM batches b
                    JOIN batch_materials bm ON bm.batch_id = b.id
                    WHERE bm.material_lot = ?
                    GROUP BY b.id
                    """,
                    (lot_no,),
                ).fetchall()

                # 每条产线从最早的直接受影响批次起重算
                start_by_line: dict[str, int] = {}
                directly_affected: set[int] = set()
                for r in affected_rows:
                    directly_affected.add(r["id"])
                    start_by_line[r["line_code"]] = min(
                        start_by_line.get(r["line_code"], r["seq"]), r["seq"]
                    )

                recalculated: list[int] = []
                for line_code in sorted(start_by_line):
                    recalculated.extend(
                        _recompute_from(
                            conn,
                            line_code,
                            start_by_line[line_code],
                            material_correction_id=event_id,
                            directly_affected=directly_affected,
                        )
                    )

                conn.execute(
                    "UPDATE material_correction_events SET recalculated=? WHERE id=?",
                    (json.dumps(recalculated), event_id),
                )

                if recalculated:
                    placeholders = ",".join("?" for _ in recalculated)
                    batch_rows = conn.execute(
                        f"SELECT * FROM batches WHERE id IN ({placeholders})"
                        " ORDER BY line_code, seq",
                        recalculated,
                    ).fetchall()
                    summaries = [
                        {
                            "id": r["id"],
                            "line_code": r["line_code"],
                            "batch_no": r["batch_no"],
                            "seq": r["seq"],
                            "decision": _latest_verdict(conn, r["id"])["decision"],
                        }
                        for r in batch_rows
                    ]
                else:
                    summaries = []

            result = get_material(lot_no)
            result["material_correction_event_id"] = event_id
            result["recalculated_batch_ids"] = recalculated
            result["recalculated_batches"] = summaries
            return result
        finally:
            conn.close()


# ---------------------------------------------------------------- 批次登记

def register_batch(payload: dict[str, Any]) -> dict[str, Any]:
    line_code = (payload.get("line_code") or config.DEFAULT_LINE).strip()
    batch_no = payload["batch_no"].strip()
    material_lots = _dedupe_preserve(
        [lot.strip() for lot in payload["material_lots"]]
    )
    declared = _normalize(payload.get("declared_allergens"))
    cleaning = _normalize(payload.get("cleaning"))

    with _WRITER_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                _get_or_create_line(conn, line_code)

                dup = conn.execute(
                    "SELECT 1 FROM batches WHERE line_code=? AND batch_no=?",
                    (line_code, batch_no),
                ).fetchone()
                if dup:
                    raise ApiError(
                        409,
                        "DUPLICATE_BATCH",
                        f"产线 {line_code} 上批次号已存在: {batch_no}",
                    )

                # 未知原料批号：整笔拒绝
                if material_lots:
                    placeholders = ",".join("?" for _ in material_lots)
                    found = {
                        r["lot_no"]
                        for r in conn.execute(
                            f"SELECT lot_no FROM material_lots WHERE lot_no IN ({placeholders})",
                            material_lots,
                        ).fetchall()
                    }
                else:
                    found = set()
                unknown = [lot for lot in material_lots if lot not in found]
                if unknown:
                    raise ApiError(
                        400,
                        "UNKNOWN_MATERIAL",
                        f"未知原料批号: {'、'.join(unknown)}（请先登记原料）",
                    )

                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM batches WHERE line_code=?",
                    (line_code,),
                ).fetchone()
                seq = seq_row["s"] + 1

                cur = conn.execute(
                    "INSERT INTO batches(line_code, batch_no, seq, declared_allergens,"
                    " cleaning, created_at) VALUES (?,?,?,?,?,?)",
                    (
                        line_code, batch_no, seq,
                        json.dumps(declared, ensure_ascii=False),
                        json.dumps(cleaning, ensure_ascii=False),
                        _now(),
                    ),
                )
                batch_id = cur.lastrowid
                for position, lot in enumerate(material_lots, start=1):
                    conn.execute(
                        "INSERT INTO batch_materials(batch_id, position, material_lot)"
                        " VALUES (?,?,?)",
                        (batch_id, position, lot),
                    )

                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                _recompute_from(conn, line_code, seq)
            return get_batch(batch_id)
        finally:
            conn.close()


# ---------------------------------------------------------------- 批次更正

def correct_batch(batch_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    with _WRITER_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise ApiError(
                        404, "BATCH_NOT_FOUND", f"更正目标不存在: 批次 id={batch_id}"
                    )

                # ---- 先完成全部校验，任何失败都在改动前中止 ----
                new_materials: list[str] | None = None
                new_declared: list[str] | None = None
                new_cleaning: list[str] | None = None
                changed: list[str] = []

                if payload.get("material_lots") is not None:
                    new_materials = _dedupe_preserve(
                        [lot.strip() for lot in payload["material_lots"]]
                    )
                    placeholders = ",".join("?" for _ in new_materials)
                    found = {
                        r["lot_no"]
                        for r in conn.execute(
                            f"SELECT lot_no FROM material_lots WHERE lot_no IN ({placeholders})",
                            new_materials,
                        ).fetchall()
                    }
                    unknown = [lot for lot in new_materials if lot not in found]
                    if unknown:
                        raise ApiError(
                            400,
                            "UNKNOWN_MATERIAL",
                            f"未知原料批号: {'、'.join(unknown)}（请先登记原料）",
                        )
                    changed.append("material_lots")

                if payload.get("declared_allergens") is not None:
                    new_declared = _normalize(payload["declared_allergens"])
                    changed.append("declared_allergens")

                if payload.get("cleaning") is not None:
                    new_cleaning = _normalize(payload["cleaning"])
                    changed.append("cleaning")

                if not changed:
                    raise ApiError(
                        400,
                        "NO_CHANGES",
                        "未提供任何可更正字段（material_lots / declared_allergens / cleaning）",
                    )

                old_materials = [
                    r["material_lot"]
                    for r in conn.execute(
                        "SELECT material_lot FROM batch_materials WHERE batch_id=?"
                        " ORDER BY position",
                        (batch_id,),
                    ).fetchall()
                ]
                old_values: dict[str, Any] = {
                    "material_lots": old_materials,
                    "declared_allergens": _loads(batch["declared_allergens"]),
                    "cleaning": _loads(batch["cleaning"]),
                }

                # ---- 应用更正 ----
                if new_declared is not None:
                    conn.execute(
                        "UPDATE batches SET declared_allergens=? WHERE id=?",
                        (json.dumps(new_declared, ensure_ascii=False), batch_id),
                    )
                if new_cleaning is not None:
                    conn.execute(
                        "UPDATE batches SET cleaning=? WHERE id=?",
                        (json.dumps(new_cleaning, ensure_ascii=False), batch_id),
                    )
                if new_materials is not None:
                    conn.execute(
                        "DELETE FROM batch_materials WHERE batch_id=?", (batch_id,)
                    )
                    for position, lot in enumerate(new_materials, start=1):
                        conn.execute(
                            "INSERT INTO batch_materials(batch_id, position, material_lot)"
                            " VALUES (?,?,?)",
                            (batch_id, position, lot),
                        )

                new_values = {
                    "material_lots": new_materials if new_materials is not None else old_materials,
                    "declared_allergens": new_declared
                    if new_declared is not None
                    else _loads(batch["declared_allergens"]),
                    "cleaning": new_cleaning
                    if new_cleaning is not None
                    else _loads(batch["cleaning"]),
                }

                cur = conn.execute(
                    "INSERT INTO correction_events(batch_id, changed_fields, old_values,"
                    " new_values, created_at) VALUES (?,?,?,?,?)",
                    (
                        batch_id,
                        json.dumps(changed, ensure_ascii=False),
                        json.dumps(old_values, ensure_ascii=False),
                        json.dumps(new_values, ensure_ascii=False),
                        _now(),
                    ),
                )
                correction_id = cur.lastrowid

                # ---- 从被更正批次起按产线顺序重算 ----
                recalculated = _recompute_from(
                    conn, batch["line_code"], batch["seq"], correction_id
                )
                conn.execute(
                    "UPDATE correction_events SET recalculated=? WHERE id=?",
                    (json.dumps(recalculated), correction_id),
                )

            result = get_batch(batch_id)
            result["recalculated_batch_ids"] = recalculated
            result["correction_event_id"] = correction_id
            return result
        finally:
            conn.close()


# ---------------------------------------------------------------- 查询

def _serialize_verdict(row: sqlite3.Row) -> dict[str, Any]:
    arrays = {
        "incoming_residual": _loads(row["incoming_residual"]),
        "cleaning_covered": _loads(row["cleaning_covered"]),
        "remaining_carryover": _loads(row["remaining_carryover"]),
        "material_allergens": _loads(row["material_allergens"]),
        "actual_allergens": _loads(row["actual_allergens"]),
        "undeclared_allergens": _loads(row["undeclared_allergens"]),
    }
    codes = sorted({c for values in arrays.values() for c in values})
    return {
        "version": row["version"],
        **arrays,
        "sources": _loads(row["sources"]),
        "decision": row["decision"],
        "reason": row["reason"],
        "triggered_by": row["triggered_by"],
        "correction_event_id": row["correction_id"],
        "material_correction_event_id": row["material_correction_id"],
        "created_at": row["created_at"],
        "allergen_names": {code: display_name(code) for code in codes},
    }


def _serialize_batch(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    materials = [
        {
            "lot_no": r["material_lot"],
            "material_name": r["material_name"],
            "allergens": _loads(r["allergens"]),
        }
        for r in conn.execute(
            """
            SELECT bm.material_lot AS material_lot, ml.material_name AS material_name,
                   ml.allergens AS allergens
            FROM batch_materials bm
            JOIN material_lots ml ON ml.lot_no = bm.material_lot
            WHERE bm.batch_id = ?
            ORDER BY bm.position
            """,
            (row["id"],),
        ).fetchall()
    ]
    verdict_row = _latest_verdict(conn, row["id"])
    version_count = conn.execute(
        "SELECT COUNT(*) AS c FROM verdict_versions WHERE batch_id=?", (row["id"],)
    ).fetchone()["c"]
    return {
        "id": row["id"],
        "line_code": row["line_code"],
        "batch_no": row["batch_no"],
        "seq": row["seq"],
        "material_lots": [m["lot_no"] for m in materials],
        "materials": materials,
        "declared_allergens": _loads(row["declared_allergens"]),
        "cleaning": _loads(row["cleaning"]),
        "created_at": row["created_at"],
        "verdict": _serialize_verdict(verdict_row) if verdict_row else None,
        "verdict_version_count": version_count,
    }


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ApiError(404, "BATCH_NOT_FOUND", f"批次不存在: id={batch_id}")
        return _serialize_batch(conn, row)
    finally:
        conn.close()


def list_batches(line_code: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        if line_code:
            rows = conn.execute(
                "SELECT * FROM batches WHERE line_code=? ORDER BY seq", (line_code,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY line_code, seq"
            ).fetchall()
        return [_serialize_batch(conn, r) for r in rows]
    finally:
        conn.close()


def get_history(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT id, line_code, batch_no, seq FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ApiError(404, "BATCH_NOT_FOUND", f"批次不存在: id={batch_id}")
        rows = conn.execute(
            "SELECT * FROM verdict_versions WHERE batch_id=? ORDER BY version",
            (batch_id,),
        ).fetchall()
        return {
            "batch": {
                "id": batch["id"],
                "line_code": batch["line_code"],
                "batch_no": batch["batch_no"],
                "seq": batch["seq"],
            },
            "versions": [_serialize_verdict(r) for r in rows],
        }
    finally:
        conn.close()


def list_corrections(batch_id: int | None = None) -> list[dict[str, Any]]:
    """批次更正审计列表（可按批次过滤）。"""
    conn = connect()
    try:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM correction_events WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM correction_events ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "batch_id": r["batch_id"],
                "changed_fields": _loads(r["changed_fields"]),
                "old_values": _loads(r["old_values"]),
                "new_values": _loads(r["new_values"]),
                "recalculated_batch_ids": _loads(r["recalculated"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_material_corrections(lot_no: str | None = None) -> list[dict[str, Any]]:
    """原料批号主数据更正审计列表（可按批号过滤）。"""
    conn = connect()
    try:
        if lot_no is not None:
            rows = conn.execute(
                "SELECT * FROM material_correction_events WHERE lot_no=? ORDER BY id",
                (lot_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM material_correction_events ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "lot_no": r["lot_no"],
                "changed_fields": _loads(r["changed_fields"]),
                "old_values": _loads(r["old_values"]),
                "new_values": _loads(r["new_values"]),
                "recalculated_batch_ids": _loads(r["recalculated"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_lines() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute("SELECT code FROM lines ORDER BY code").fetchall()
        return [_line_status(conn, r["code"]) for r in rows]
    finally:
        conn.close()


def get_line(line_code: str) -> dict[str, Any]:
    conn = connect()
    try:
        exists = conn.execute("SELECT 1 FROM lines WHERE code=?", (line_code,)).fetchone()
        if not exists:
            raise ApiError(404, "LINE_NOT_FOUND", f"产线不存在: {line_code}")
        return _line_status(conn, line_code)
    finally:
        conn.close()


def _line_status(conn: sqlite3.Connection, line_code: str) -> dict[str, Any]:
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE line_code=?", (line_code,)
    ).fetchone()["c"]
    last = conn.execute(
        "SELECT * FROM batches WHERE line_code=? ORDER BY seq DESC LIMIT 1",
        (line_code,),
    ).fetchone()
    residual: list[str] = []
    last_summary = None
    if last is not None:
        verdict = _latest_verdict(conn, last["id"])
        if verdict is not None:
            residual = _loads(verdict["actual_allergens"])
        last_summary = {
            "id": last["id"],
            "batch_no": last["batch_no"],
            "seq": last["seq"],
            "decision": verdict["decision"] if verdict else None,
            "actual_allergens": residual,
        }
    return {
        "line_code": line_code,
        "batch_count": count,
        "last_batch": last_summary,
        "residual_allergens": residual,
        "residual_allergen_names": [display_name(c) for c in residual],
    }
