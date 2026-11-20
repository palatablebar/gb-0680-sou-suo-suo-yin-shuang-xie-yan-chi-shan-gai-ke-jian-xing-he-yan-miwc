"""字段修订样例的端到端裁决：页面、差异定责、归档共用同一份正文。"""
from app.pipeline import calculate, execute, load_sample
from app import store


def _patch_run():
    return calculate(load_sample("patches.csv"))


def _body(run, doc_id, side):
    return next(b for b in run["stages"][2]["bodies"] if b["doc_id"] == doc_id and b["side"] == side)


def _doc(run, doc_id):
    return next(r for r in run["stages"][3]["rows"] if r["doc_id"] == doc_id)


def test_sample_materializes_unique_bodies():
    run = _patch_run()
    m3 = run["stages"][2]["metrics"]
    # P01/P06 双端成正文，P02 源端正文，其余阻断或单侧
    assert m3["blocked"] >= 4
    assert m3["no_baseline"] >= 2
    assert m3["version_gap"] >= 1
    assert m3["body_conflict_rows"] == 2


def test_out_of_order_chain_canonical_body():
    "P01 乱序补丁重放：tag 被撤下、price 更新、stock 更新"
    run = _patch_run()
    source = _body(run, "P01", "source")
    assert source["canonical_body"] == {"name": "甲", "price": "12.00", "stock": "8"}
    assert source["replayed_versions"] == ["1", "2", "3"]


def test_removed_field_not_resurrected_on_index():
    "P02 索引迟到旧补丁不能让已撤下的 price 回流"
    run = _patch_run()
    source = _body(run, "P02", "source")
    index = _body(run, "P02", "index")
    assert "price" not in source["canonical_body"]
    # 索引链没有撤下 price 的 v2/null，其正文仍含 price——差异必须被定责
    doc = _doc(run, "P02")
    assert doc["body_blocked"] is False
    assert "price" in doc["index_extra_fields"]
    assert index["canonical_body"]["price"] == "21.00"


def test_missing_baseline_keeps_original_state():
    run = _patch_run()
    doc = _doc(run, "P04")
    assert doc["body_blocked"] is True
    assert doc["block_reason"] == "NO_BASELINE"


def test_version_gap_blocked_and_gate_not_passed():
    run = _patch_run()
    doc = _doc(run, "P03")
    assert doc["body_blocked"] is True and doc["block_reason"] == "VERSION_GAP"
    gate = next(r for r in run["stages"][4]["rows"] if r["doc_id"] == "P03")
    assert gate["gate"] == "正文阻断" and gate["reported"] == "0"


def test_same_version_conflict_subject_blocked():
    run = _patch_run()
    doc = _doc(run, "P05")
    assert doc["body_blocked"] is True
    assert doc["block_reason"] == "SAME_VERSION_CONFLICT"
    assert any(q["reason"] == "SAME_VERSION_CONFLICT" for q in run["stages"][2]["quarantine"])


def test_page_diff_archive_share_one_body():
    "M4 行内正文必须与 M3 裁决正文逐字节一致，归档封存同一结果"
    run = _patch_run()
    for row in run["stages"][3]["rows"]:
        source = _body(run, row["doc_id"], "source") if any(
            b["doc_id"] == row["doc_id"] and b["side"] == "source" for b in run["stages"][2]["bodies"]
        ) else None
        if source:
            assert row["source_canonical_body"] == source["canonical_body"]
    assert run["stages"][6]["metrics"]["body_sha256"]
    # 归档行就是 M6/M5 透传的同一批行，没有二次重建
    assert run["stages"][6]["rows"] == run["stages"][5]["rows"]


def test_replay_same_batch_is_idempotent():
    "重放同一批：run_id 相同、正文不变、不新增记录"
    raw = load_sample("patches.csv")
    first = execute(raw)
    second = execute(raw)
    assert first["run_id"] == second["run_id"]
    assert len(store.history()) == 1
    a = first["stages"][2]["bodies"]
    b = second["stages"][2]["bodies"]
    assert a == b


def test_blocked_documents_remain_in_archive_with_reason():
    import csv
    import io
    from app.archive import build_archive
    run = execute(load_sample("patches.csv"))
    text = build_archive(run).lstrip("﻿")
    evidence = "\n".join(text.splitlines())
    assert "VERSION_GAP" in evidence and "NO_BASELINE" in evidence
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "run_id"
