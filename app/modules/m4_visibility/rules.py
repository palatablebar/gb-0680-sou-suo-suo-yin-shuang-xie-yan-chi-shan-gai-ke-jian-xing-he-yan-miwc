from collections import defaultdict
from app.reasons import sources
from app.values import number
import json
def same_document(a,b):return json.dumps(a,sort_keys=True)==json.dumps(b,sort_keys=True)
def field_diff(source_body,index_body):
    """字段级差异定责，以源正文为基准看索引要做什么：
    - index_extra_fields: 索引多出（源已撤下而索引仍持有，即回流风险）；
    - index_missing_fields: 索引缺失；
    - index_changed_fields: 同字段取值不一致。
    全部取自同一份裁决正文。"""
    source_body=source_body or {};index_body=index_body or {}
    return {
        "index_extra_fields":sorted(set(index_body)-set(source_body)),
        "index_missing_fields":sorted(set(source_body)-set(index_body)),
        "index_changed_fields":sorted(k for k in set(source_body)&set(index_body) if source_body[k]!=index_body[k]),
    }
def evaluate(rows,context):
    body_index={}
    stage3=(context or {}).get("versions")
    if isinstance(stage3,dict):
        body_index={(b["doc_id"],b["side"]):b for b in stage3.get("bodies",[])}
    grouped=defaultdict(dict)
    for row in rows:grouped[row["doc_id"]][row["side"]]=row
    out=[]
    for doc,sides in sorted(grouped.items()):
        source=sides.get("source");target=sides.get("index");lines=[n for r in sides.values() for n in sources(r)]
        if source is None:state="ORPHAN_INDEX"
        elif target is None:state="MISSING_INDEX" if source["op"]=="upsert" else "DELETE_NOT_CONFIRMED"
        elif number(target["version"])>number(source["version"]):state="INDEX_AHEAD"
        elif number(target["version"])<number(source["version"]):state="STALE_INDEX"
        elif source["op"]!=target["op"]:state="OP_MISMATCH"
        elif source["op"]=="upsert" and not same_document(source.get("document"),target.get("document")):state="CONTENT_MISMATCH"
        else:state="CONSISTENT"
        anchor=source or target
        source_body=body_index.get((doc,"source"))
        index_body=body_index.get((doc,"index"))
        # 无上下文（单元预览）时回退为事件自带整篇文档，保证独立可运行。
        source_canonical=source_body["canonical_body"] if source_body else (source.get("document") if source else None)
        index_canonical=index_body["canonical_body"] if index_body else (target.get("document") if target else None)
        source_state=source_body["body_state"] if source_body else ("TOMBSTONE" if not source or source["op"]=="delete" else "CANONICAL")
        index_state=index_body["body_state"] if index_body else ("TOMBSTONE" if not target or target["op"]=="delete" else "CANONICAL")
        block_reasons=[b for b in (source_body,index_body) if b and b["body_state"]=="BLOCKED"]
        blocked=bool(block_reasons)
        diff=field_diff(source_canonical,index_canonical) if (source_canonical is not None and index_canonical is not None) else {"index_extra_fields":[],"index_missing_fields":[],"index_changed_fields":[]}
        body_equal=(not blocked) and same_document(source_canonical,index_canonical) if (source_canonical is not None or index_canonical is not None) else False
        out.append({**anchor,"_sources":lines,"source_version":source["version"] if source else "—","index_version":target["version"] if target else "—","source_op":source["op"] if source else "—","index_op":target["op"] if target else "—","visible_at":target.get("visible_at") if target else None,"visibility_state":state,"expected":"1","reported":"1" if state=="CONSISTENT" else "0","document_equal":state=="CONSISTENT","source_body_state":source_state,"index_body_state":index_state,"body_blocked":blocked,"block_reason":block_reasons[0]["block_reason"] if block_reasons else None,"block_detail":block_reasons[0]["block_detail"] if block_reasons else None,"source_canonical_body":source_canonical,"index_canonical_body":index_canonical,"body_equal":body_equal,"index_extra_fields":diff["index_extra_fields"],"index_missing_fields":diff["index_missing_fields"],"index_changed_fields":diff["index_changed_fields"]})
    body_ready=sum(r["source_body_state"]=="CANONICAL" and r["index_body_state"]=="CANONICAL" for r in out)
    return {"rows":out,"quarantine":[],"metrics":{"documents":len(out),"consistent":sum(r["visibility_state"]=="CONSISTENT" for r in out),"missing":sum(r["visibility_state"] in {"MISSING_INDEX","DELETE_NOT_CONFIRMED"} for r in out),"body_ready":body_ready,"body_blocked":sum(r["body_blocked"] for r in out),"body_equal":sum(r["body_equal"] for r in out)}}
