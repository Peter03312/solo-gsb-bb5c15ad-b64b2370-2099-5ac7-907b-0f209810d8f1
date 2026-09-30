"""核心业务规则测试：残留传递、清洁覆盖、扣留级联、更正重算与历史版本。"""
from __future__ import annotations

import pytest

from conftest import make_material


def _register(svc, batch_no, lots, declared, cleaning, line="L1"):
    return svc.register_batch(
        {
            "batch_no": batch_no,
            "line_code": line,
            "material_lots": lots,
            "declared_allergens": declared,
            "cleaning": cleaning,
        }
    )


def test_first_batch_release_when_all_declared(svc):
    make_material(svc, "RM-MILK-01", ["乳"], "牛奶")
    b = _register(svc, "B1", ["RM-MILK-01"], ["MILK"], [])
    v = b["verdict"]
    assert v["decision"] == "RELEASE"
    assert v["actual_allergens"] == ["MILK"]
    assert v["undeclared_allergens"] == []
    assert v["material_allergens"] == ["MILK"]
    assert v["incoming_residual"] == []


def test_undeclared_allergen_withholds(svc):
    make_material(svc, "RM-EGG-01", ["EGG"], "蛋液")
    b = _register(svc, "B1", ["RM-EGG-01"], [], [])
    assert b["verdict"]["decision"] == "WITHHELD"
    assert b["verdict"]["undeclared_allergens"] == ["EGG"]
    assert "扣留" in b["verdict"]["reason"]


def test_chinese_alias_and_code_equivalent(svc):
    make_material(svc, "RM-A", ["花生"], "花生酱")
    b = _register(svc, "B1", ["RM-A"], ["PEANUT"], [])
    assert b["verdict"]["decision"] == "RELEASE"


def test_carryover_only_cleaned_items_cleared(svc):
    """上一批残留 MILK+EGG；本次清洁只核验清除 MILK => EGG 仍残留。"""
    make_material(svc, "RM-MIX", ["MILK", "EGG"], "蛋奶糊")
    make_material(svc, "RM-CLEAN", ["SOY"], "大豆油")

    b1 = _register(svc, "B1", ["RM-MIX"], ["MILK", "EGG"], [])
    assert b1["verdict"]["decision"] == "RELEASE"

    # B2 原料只含 SOY 且仅声明 SOY；清洁只清除 MILK
    b2 = _register(svc, "B2", ["RM-CLEAN"], ["SOY"], ["MILK"])
    v2 = b2["verdict"]
    assert v2["incoming_residual"] == ["EGG", "MILK"]
    assert v2["cleaning_covered"] == ["MILK"]
    assert v2["remaining_carryover"] == ["EGG"]
    assert v2["actual_allergens"] == ["EGG", "SOY"]
    assert v2["undeclared_allergens"] == ["EGG"]
    assert v2["decision"] == "WITHHELD"


def test_withheld_batch_still_carries_forward(svc):
    """即使上一批被扣留，其实际过敏原仍继续传给下一批。"""
    make_material(svc, "RM-NUT", ["TREE_NUT"], "坚果碎")
    make_material(svc, "RM-SAFE", [], "清水")

    b1 = _register(svc, "B1", ["RM-NUT"], [], [])  # 扣留
    assert b1["verdict"]["decision"] == "WITHHELD"

    b2 = _register(svc, "B2", ["RM-SAFE"], [], [])  # 无清洁
    assert b2["verdict"]["incoming_residual"] == ["TREE_NUT"]
    assert b2["verdict"]["actual_allergens"] == ["TREE_NUT"]
    assert b2["verdict"]["decision"] == "WITHHELD"


def test_cascade_correct_cleaning_propagates_to_all_followers(svc):
    """更正某批清洁核验后，该批及后续批次全部重算。"""
    make_material(svc, "RM-MILK", ["MILK"], "牛奶")
    make_material(svc, "RM-SAFE", [], "盐水")

    b1 = _register(svc, "B1", ["RM-MILK"], ["MILK"], [])
    b2 = _register(svc, "B2", ["RM-SAFE"], [], [])
    b3 = _register(svc, "B3", ["RM-SAFE"], [], [])
    assert b2["verdict"]["decision"] == "WITHHELD"
    assert b3["verdict"]["decision"] == "WITHHELD"

    # 更正 B2：换线前清洁核验确认清除 MILK
    result = svc.correct_batch(
        b2["id"], {"cleaning": ["MILK"]}
    )
    assert result["verdict"]["decision"] == "RELEASE"
    assert result["verdict"]["remaining_carryover"] == []
    assert result["recalculated_batch_ids"] == [b2["id"], b3["id"]]

    b3_fresh = svc.get_batch(b3["id"])
    assert b3_fresh["verdict"]["incoming_residual"] == []
    assert b3_fresh["verdict"]["decision"] == "RELEASE"

    b1_fresh = svc.get_batch(b1["id"])
    assert b1_fresh["verdict"]["version"] == 1  # 更正点之前不受影响


def test_correct_material_lots_recomputes(svc):
    make_material(svc, "RM-A", ["MILK"], "A")
    make_material(svc, "RM-B", ["EGG"], "B")

    b1 = _register(svc, "B1", ["RM-A"], ["MILK"], [])
    assert b1["verdict"]["decision"] == "RELEASE"

    result = svc.correct_batch(b1["id"], {"material_lots": ["RM-B"]})
    assert result["material_lots"] == ["RM-B"]
    assert result["verdict"]["material_allergens"] == ["EGG"]
    assert result["verdict"]["undeclared_allergens"] == ["EGG"]
    assert result["verdict"]["decision"] == "WITHHELD"


def test_history_versions_are_appended(svc):
    make_material(svc, "RM-A", ["MILK"], "A")
    b1 = _register(svc, "B1", ["RM-A"], [], [])
    svc.correct_batch(b1["id"], {"declared_allergens": ["MILK"]})

    history = svc.get_history(b1["id"])
    assert len(history["versions"]) == 2
    assert history["versions"][0]["version"] == 1
    assert history["versions"][0]["decision"] == "WITHHELD"
    assert history["versions"][0]["triggered_by"] == "REGISTER"
    assert history["versions"][1]["version"] == 2
    assert history["versions"][1]["decision"] == "RELEASE"
    assert history["versions"][1]["triggered_by"] == "CORRECTION"


def test_line_residual_reflects_last_batch(svc):
    make_material(svc, "RM-A", ["MILK", "EGG"], "A")
    _register(svc, "B1", ["RM-A"], ["MILK", "EGG"], [])
    line = svc.get_line("L1")
    assert line["residual_allergens"] == ["EGG", "MILK"]
    assert line["last_batch"]["decision"] == "RELEASE"


def test_lines_are_independent(svc):
    make_material(svc, "RM-A", ["MILK"], "A")
    _register(svc, "B1", ["RM-A"], ["MILK"], [], line="L1")
    b2 = _register(svc, "B1", ["RM-A"], [], [], line="L2")
    assert b2["verdict"]["incoming_residual"] == []


# ---------------------------------------------------------------- 拒绝场景

def test_duplicate_batch_rejected(svc):
    make_material(svc, "RM-A", [], "A")
    _register(svc, "B1", ["RM-A"], [], [])
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        _register(svc, "B1", ["RM-A"], [], [])
    assert exc.value.status_code == 409
    assert exc.value.code == "DUPLICATE_BATCH"


def test_unknown_material_rejected(svc):
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        _register(svc, "B1", ["NOPE"], [], [])
    assert exc.value.status_code == 400
    assert exc.value.code == "UNKNOWN_MATERIAL"


def test_invalid_allergen_rejected(svc):
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        make_material(svc, "RM-A", ["星际尘埃"], "A")
    assert exc.value.status_code == 400
    assert exc.value.code == "INVALID_ALLERGEN"


def test_correction_target_missing_rejected(svc):
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        svc.correct_batch(9999, {"cleaning": ["MILK"]})
    assert exc.value.status_code == 404
    assert exc.value.code == "BATCH_NOT_FOUND"


def test_failed_registration_leaves_no_partial_change(svc):
    """未知原料导致登记失败时，批次号/seq 均不得残留。"""
    make_material(svc, "RM-A", [], "A")
    from app.services import ApiError
    from app.database import connect
    from app import config

    with pytest.raises(ApiError):
        _register(svc, "B1", ["RM-A", "MISSING"], [], [])

    conn = connect(config.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM verdict_versions").fetchone()["c"] == 0
        # 失败后同批次号仍可正常登记（无 seq 被占用）
    finally:
        conn.close()

    b = _register(svc, "B1", ["RM-A"], [], [])
    assert b["seq"] == 1


def test_failed_correction_leaves_no_partial_change(svc):
    make_material(svc, "RM-A", ["MILK"], "A")
    make_material(svc, "RM-B", ["EGG"], "B")
    b1 = _register(svc, "B1", ["RM-A"], ["MILK"], [])

    from app.services import ApiError

    # 更正到不存在的原料批号：必须整体失败，原裁决不变、无新版本
    with pytest.raises(ApiError) as exc:
        svc.correct_batch(b1["id"], {"material_lots": ["MISSING"]})
    assert exc.value.code == "UNKNOWN_MATERIAL"

    fresh = svc.get_batch(b1["id"])
    assert fresh["material_lots"] == ["RM-A"]
    assert fresh["verdict_version_count"] == 1


def test_material_master_correction_cascades_across_lines(svc):
    """原料批号过敏原更正：使用它的批次跨产线重算，未使用的不受影响。"""
    make_material(svc, "RM-A", ["MILK"], "A")
    make_material(svc, "RM-X", [], "X")

    # L1: B1 用 RM-A（放行），B2 不用
    b1 = _register(svc, "B1", ["RM-A"], ["MILK"], [], line="L1")
    b2 = _register(svc, "B2", ["RM-X"], [], [], line="L1")
    # L2: B3 用 RM-A 且未声明 EGG（更正前放行）
    b3 = _register(svc, "B3", ["RM-A"], ["MILK"], [], line="L2")

    # 更正 RM-A：MILK -> MILK + EGG
    result = svc.correct_material("RM-A", {"allergens": ["MILK", "EGG"]})
    assert set(result["recalculated_batch_ids"]) == {b1["id"], b2["id"], b3["id"]}

    fresh_b1 = svc.get_batch(b1["id"])
    assert fresh_b1["verdict"]["version"] == 2
    assert fresh_b1["verdict"]["decision"] == "WITHHELD"  # EGG 未声明
    assert fresh_b1["verdict"]["material_allergens"] == ["EGG", "MILK"]
    assert fresh_b1["verdict"]["triggered_by"] == "MATERIAL_CORRECTION"

    fresh_b2 = svc.get_batch(b2["id"])
    # B2 本身没用 RM-A，但接收 B1 的新残留 EGG（无清洁）-> 级联扣留
    assert fresh_b2["verdict"]["version"] == 2
    assert fresh_b2["verdict"]["decision"] == "WITHHELD"
    assert fresh_b2["verdict"]["incoming_residual"] == ["EGG", "MILK"]
    assert fresh_b2["verdict"]["triggered_by"] == "RECASCADE"

    fresh_b3 = svc.get_batch(b3["id"])
    assert fresh_b3["verdict"]["version"] == 2
    assert fresh_b3["verdict"]["undeclared_allergens"] == ["EGG"]

    # 审计可查
    events = svc.list_material_corrections("RM-A")
    assert len(events) == 1
    assert events[0]["old_values"]["allergens"] == ["MILK"]
    assert events[0]["new_values"]["allergens"] == ["EGG", "MILK"]

    # 历史版本保留
    hist = svc.get_history(b1["id"])
    assert [v["version"] for v in hist["versions"]] == [1, 2]
    assert hist["versions"][0]["decision"] == "RELEASE"
    assert hist["versions"][1]["material_correction_event_id"] == result[
        "material_correction_event_id"
    ]


def test_material_correction_removing_allergen_releases_downstream(svc):
    make_material(svc, "RM-A", ["MILK", "EGG"], "A")
    b1 = _register(svc, "B1", ["RM-A"], ["MILK"], [])  # EGG 未声明 -> 扣留
    assert b1["verdict"]["decision"] == "WITHHELD"

    result = svc.correct_material("RM-A", {"allergens": ["MILK"]})
    assert result["recalculated_batches"][0]["decision"] == "RELEASE"


def test_material_correction_unknown_target_rejected(svc):
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        svc.correct_material("GHOST", {"allergens": ["MILK"]})
    assert exc.value.status_code == 404
    assert exc.value.code == "MATERIAL_NOT_FOUND"


def test_material_correction_invalid_allergen_no_partial_change(svc):
    make_material(svc, "RM-A", ["MILK"], "A")
    b1 = _register(svc, "B1", ["RM-A"], ["MILK"], [])
    from app.services import ApiError

    with pytest.raises(ApiError) as exc:
        svc.correct_material("RM-A", {"allergens": ["幻之过敏原"]})
    assert exc.value.code == "INVALID_ALLERGEN"

    # 无新版本、无审计事件、主数据不变
    assert svc.get_batch(b1["id"])["verdict_version_count"] == 1
    assert svc.list_material_corrections("RM-A") == []
    assert svc.get_material("RM-A")["allergens"] == ["MILK"]
