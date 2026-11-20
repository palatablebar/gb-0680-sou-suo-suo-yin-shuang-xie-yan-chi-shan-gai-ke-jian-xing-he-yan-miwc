"""字段级修订正文裁决。

裁决每一个 (doc_id, side) 主体在当前输入批次下能否形成“唯一可检索正文”。

朴素做法的两个陷阱：
- 直接把本次补丁盖到索引正文：撤下过的字段会随迟到旧补丁回流；
- 只按本次补丁涉及字段重建：未提及的字段发生数据丢失。

本模块只认一条规则：从可追溯的整篇 upsert 基线出发，按版本号升序重放
基线上的全部 upsert / patch / delete 事件。基线缺失、版本链不连续或
同版修订冲突时不形成新正文，冻结上一可用正文并登记阻断原因。
"""
from collections import defaultdict

from app.reasons import reject, sources
from app.values import number

PATCH = "patch"
UPSERT = "upsert"
DELETE = "delete"

# 阻断原因 -> 人读说明
BLOCK_REASONS = {
    "NO_BASELINE": "缺少可追溯的整篇正文基线，字段补丁无法重放",
    "VERSION_GAP": "基线到当前版本之间存在缺失版本，正文链不连续",
    "SAME_VERSION_CONFLICT": "同文档同端同版本修订内容冲突，不回落旧版本",
}


def _version_key(row):
    return number(row["version"])


def _integral(value):
    return int(value.to_integral_value())


def _apply_patch(body, patch):
    """只改补丁提及的字段；JSON null 表示字段墓碑，撤下即移除。"""
    result = dict(body)
    for field, value in patch.items():
        if value is None:
            result.pop(field, None)
        else:
            result[field] = value
    return result


def materialize(rows):
    """对 M2 之后的事件行重放正文。

    返回 (bodies, quarantine)：
    - bodies: 每个 (doc_id, side) 一条裁决，含 canonical_body / body_state /
      block_reason / replayed_versions / body_sources，顺序确定；
    - quarantine: 同版冲突事件，沿用全局隔离行结构。
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["doc_id"], row["side"])].append(row)

    bodies = []
    quarantine = []

    for (doc_id, side), events in sorted(grouped.items()):
        lineage = sorted(
            events, key=lambda e: (_version_key(e), str(e.get("event_id", "")))
        )
        all_sources = [n for event in lineage for n in sources(event)]
        by_version = defaultdict(list)
        for event in lineage:
            by_version[event["version"]].append(event)

        verdict = {
            "doc_id": doc_id,
            "side": side,
            "head_version": lineage[-1]["version"],
            "head_op": lineage[-1]["op"],
            "canonical_body": None,
            "body_state": "BLOCKED",
            "block_reason": None,
            "block_detail": None,
            "baseline_version": None,
            "replayed_versions": [],
            "superseded_versions": [],
            "body_sources": all_sources,
            "_sources": all_sources,
        }

        # 规则三：同版修订冲突，整主体隔离，保留原状态（此时尚无新正文）。
        conflicts = []
        for version, same in sorted(by_version.items(), key=lambda kv: number(kv[0])):
            signatures = {
                _fingerprint(event)
                for event in same
            }
            if len(signatures) > 1:
                conflicts.extend(same)
        if conflicts:
            quarantine.extend(
                reject(event, "SAME_VERSION_CONFLICT", BLOCK_REASONS["SAME_VERSION_CONFLICT"])
                for event in conflicts
            )
            verdict["block_reason"] = "SAME_VERSION_CONFLICT"
            verdict["block_detail"] = BLOCK_REASONS["SAME_VERSION_CONFLICT"]
            bodies.append(verdict)
            continue

        # 同版同内容合并：每个版本只保留一条代表事件。
        representatives = [same[0] for _, same in sorted(
            by_version.items(), key=lambda kv: number(kv[0])
        )]

        # 规则一：基线是链上最高版本的整篇 upsert 快照。
        baseline_index = None
        for index in range(len(representatives) - 1, -1, -1):
            if representatives[index]["op"] == UPSERT:
                baseline_index = index
                break

        head_is_delete = representatives[-1]["op"] == DELETE
        if baseline_index is None:
            # 纯删除链沿用墓碑语义；纯补丁链无基线则阻断。
            if head_is_delete:
                verdict.update(
                    body_state="TOMBSTONE",
                    canonical_body=None,
                    replayed_versions=[e["version"] for e in representatives],
                )
            else:
                verdict.update(
                    block_reason="NO_BASELINE",
                    block_detail=BLOCK_REASONS["NO_BASELINE"],
                    replayed_versions=[e["version"] for e in representatives],
                )
            bodies.append(verdict)
            continue

        baseline = representatives[baseline_index]
        verdict["baseline_version"] = baseline["version"]
        verdict["superseded_versions"] = [
            event["version"] for event in representatives[:baseline_index]
        ]
        chain = representatives[baseline_index:]
        verdict["replayed_versions"] = [event["version"] for event in chain]

        # 规则二：基线版本到链首的整数版本必须连续，缺版即阻断，
        # 冻结基线正文（上一可用正文），不把任何补丁盖上去。
        baseline_num = _version_key(baseline)
        head_num = _version_key(chain[-1])
        present = {_integral(_version_key(event)) for event in chain}
        expected = set(range(_integral(baseline_num), _integral(head_num) + 1))
        missing_versions = sorted(expected - present)
        if missing_versions:
            verdict.update(
                block_reason="VERSION_GAP",
                block_detail=BLOCK_REASONS["VERSION_GAP"]
                + "，缺失版本 "
                + ",".join(str(v) for v in missing_versions),
                canonical_body=dict(baseline["document"] or {}),
            )
            bodies.append(verdict)
            continue

        # 规则四：按版本序重放整条链，结果与事件到达顺序无关。
        body = dict(baseline["document"] or {})
        state = "CANONICAL"
        for event in chain[1:]:
            if event["op"] == UPSERT:
                body = dict(event["document"] or {})
                state = "CANONICAL"
            elif event["op"] == PATCH:
                if state == "TOMBSTONE":
                    # 文档已删，字段补丁无法在墓碑上重建正文：缺有效基线，阻断。
                    state = "BLOCKED_AFTER_TOMBSTONE"
                    break
                body = _apply_patch(body, event["document"] or {})
            elif event["op"] == DELETE:
                body = None
                state = "TOMBSTONE"

        if state == "BLOCKED_AFTER_TOMBSTONE":
            verdict.update(
                body_state="BLOCKED",
                block_reason="NO_BASELINE",
                block_detail=BLOCK_REASONS["NO_BASELINE"] + "，补丁落在删除墓碑之后",
                canonical_body=None,
            )
        elif state == "TOMBSTONE":
            verdict.update(body_state="TOMBSTONE", canonical_body=None)
        else:
            verdict.update(body_state="CANONICAL", canonical_body=body)
        bodies.append(verdict)

    metrics = {
        "bodies": len(bodies),
        "canonical": sum(b["body_state"] == "CANONICAL" for b in bodies),
        "tombstones": sum(b["body_state"] == "TOMBSTONE" for b in bodies),
        "blocked": sum(b["body_state"] == "BLOCKED" for b in bodies),
        "no_baseline": sum(b["block_reason"] == "NO_BASELINE" for b in bodies),
        "version_gap": sum(b["block_reason"] == "VERSION_GAP" for b in bodies),
        "body_conflict_rows": len(quarantine),
    }
    return bodies, quarantine, metrics


def _fingerprint(row):
    import json

    return json.dumps(
        [row["op"], row.get("document")], sort_keys=True, ensure_ascii=False
    )
