"""字段级修订正文裁决测试。

覆盖核心裁决：乱序补丁按版本序重放、字段墓碑不回流、缺基线/缺版/同版冲突
阻断并冻结原状态、重放幂等、页面与归档共用同一份正文。
"""
from app.rebody import materialize


def ev(event_id, ver, op="patch", doc="D1", side="source", document=None):
    return {
        "event_id": str(event_id),
        "doc_id": doc,
        "side": side,
        "version": str(ver),
        "op": op,
        "document": document,
        "_sources": [event_id],
    }


def baseline(ver=1, document=None, **kw):
    return ev(1, ver, "upsert", document=document or {"name": "甲", "price": "10", "tag": "新"}, **kw)


def test_out_of_order_patches_replay_by_version():
    "乱序到达不影响最终正文：按版本号升序重放整条链"
    rows = [
        ev(3, 3, document={"name": "甲2"}),
        baseline(1),
        ev(2, 2, document={"price": "12", "tag": None}),
    ]
    bodies, _, metrics = materialize(rows)
    assert len(bodies) == 1
    verdict = bodies[0]
    assert verdict["body_state"] == "CANONICAL"
    assert verdict["canonical_body"] == {"name": "甲2", "price": "12"}
    assert verdict["replayed_versions"] == ["1", "2", "3"]
    assert metrics["canonical"] == 1 and metrics["blocked"] == 0


def test_replay_is_order_independent():
    "同一批事件任意排列，裁决正文完全一致"
    rows = [baseline(1), ev(2, 2, document={"price": "12"}), ev(3, 3, document={"tag": None})]
    a, _, _ = materialize(rows)
    b, _, _ = materialize(list(reversed(rows)))
    assert a == b


def test_field_tombstone_does_not_resurrect():
    "TRAP 撤下字段回流：v2 撤下 price，迟到的旧补丁不得把 price 盖回来"
    rows = [
        baseline(1, {"name": "乙", "price": "20"}),
        ev(2, 2, document={"price": None}),
        ev(3, 3, document={"name": "乙改"}),
    ]
    verdict = materialize(rows)[0][0]
    assert verdict["canonical_body"] == {"name": "乙改"}


def test_rebuild_from_patch_only_loses_nothing_but_needs_baseline():
    "只按本次改动重建会丢字段；无基线时必须阻断而不是猜测正文"
    rows = [ev(1, 1, document={"price": "99"})]
    verdict = materialize(rows)[0][0]
    assert verdict["body_state"] == "BLOCKED"
    assert verdict["block_reason"] == "NO_BASELINE"
    assert verdict["canonical_body"] is None


def test_version_gap_freezes_last_good_body():
    "版本链断裂：冻结基线正文并登记缺失版本，不把补丁盖上"
    rows = [baseline(1, {"name": "丙", "price": "30"}), ev(2, 3, document={"price": "31"})]
    verdict = materialize(rows)[0][0]
    assert verdict["body_state"] == "BLOCKED"
    assert verdict["block_reason"] == "VERSION_GAP"
    assert "2" in verdict["block_detail"]
    # 冻结的上一可用正文仍可追溯，但不形成新的可检索正文
    assert verdict["canonical_body"] == {"name": "丙", "price": "30"}
    assert verdict["baseline_version"] == "1"


def test_same_version_conflict_blocks_whole_subject():
    "同版修订冲突：整主体隔离，不回落旧版本"
    rows = [
        baseline(1),
        ev(2, 2, document={"price": "41"}),
        ev(3, 2, document={"price": "42"}),
    ]
    bodies, quarantine, metrics = materialize(rows)
    verdict = bodies[0]
    assert verdict["body_state"] == "BLOCKED"
    assert verdict["block_reason"] == "SAME_VERSION_CONFLICT"
    assert len(quarantine) == 2
    assert all(q["reason"] == "SAME_VERSION_CONFLICT" for q in quarantine)
    assert metrics["body_conflict_rows"] == 2


def test_same_version_identical_patch_merges():
    "同版同内容合并来源，不构成冲突"
    rows = [baseline(1), ev(2, 2, document={"price": "12"}), ev(3, 2, document={"price": "12"})]
    verdict = materialize(rows)[0][0]
    assert verdict["body_state"] == "CANONICAL"
    assert verdict["canonical_body"]["price"] == "12"
    assert sorted(verdict["body_sources"]) == [1, 2, 3]


def test_delete_chain_tombstone_without_baseline():
    "纯删除链沿用墓碑语义，不误判为缺基线"
    verdict = materialize([ev(1, 1, "delete", document=None)])[0][0]
    assert verdict["body_state"] == "TOMBSTONE" and verdict["canonical_body"] is None


def test_patch_after_delete_then_upsert_rebaselines():
    "新的整篇 upsert 成为基线，旧补丁不会让已删正文复活"
    rows = [
        baseline(1, {"name": "庚", "price": "1"}),
        ev(2, 2, "delete", document=None),
        ev(3, 3, "upsert", document={"name": "庚新"}),
        ev(4, 4, document={"price": "7"}),
    ]
    verdict = materialize(rows)[0][0]
    assert verdict["baseline_version"] == "3"
    assert verdict["canonical_body"] == {"name": "庚新", "price": "7"}
    assert verdict["superseded_versions"] == ["1", "2"]


def test_patch_after_delete_without_rebaseline_blocks():
    "删除后只来字段补丁、没有新快照：不得让文档复活，按缺基线阻断"
    rows = [
        baseline(1, {"name": "辛", "price": "1"}),
        ev(2, 2, "delete", document=None),
        ev(3, 3, document={"price": "9"}),
    ]
    verdict = materialize(rows)[0][0]
    assert verdict["body_state"] == "BLOCKED"
    assert verdict["block_reason"] == "NO_BASELINE"
    assert verdict["canonical_body"] is None


def test_sides_adjudicated_independently():
    rows = [
        baseline(1, {"name": "甲"}, side="source"),
        ev(2, 2, document={"name": "甲2"}, side="source"),
        ev(3, 1, document={"only": "patch"}, side="index"),
    ]
    bodies, _, _ = materialize(rows)
    index = next(b for b in bodies if b["side"] == "index")
    source = next(b for b in bodies if b["side"] == "source")
    assert index["block_reason"] == "NO_BASELINE"
    assert source["body_state"] == "CANONICAL"
