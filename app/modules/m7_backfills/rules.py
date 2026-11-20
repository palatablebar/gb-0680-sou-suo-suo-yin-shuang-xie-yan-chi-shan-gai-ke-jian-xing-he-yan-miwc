import hashlib,json
from decimal import Decimal
from app.errors import DomainError
from app.reasons import sources
def evaluate(rows,context):
    stages=[v for v in context.values() if isinstance(v,dict) and "metrics" in v]
    balances=[s["metrics"]["balanced"] for s in stages if "balanced" in s["metrics"]]
    if balances and not all(balances): raise DomainError("UNBALANCED","来源行未对平，不能归档")
    ids=[str(r.get("doc_id",r.get("_line",""))) for r in rows]
    if len(ids)!=len(set(ids)): raise DomainError("ARCHIVE_DUPLICATE","归档业务键重复")
    encoded=json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()
    # 页面、差异定责与归档共用同一份 canonical_body；归档只封存裁决结果，
    # 正文阻断的文档保留 block_reason，不在归档侧二次改写。
    body_encoded=json.dumps(
        [[r.get("doc_id"),r.get("source_canonical_body"),r.get("index_canonical_body")] for r in rows],
        ensure_ascii=False,sort_keys=True,default=str,
    ).encode()
    return {"rows":rows,"quarantine":[],"metrics":{"records":len(rows),"source_lines":len(set(x for row in rows for x in sources(row))),"sha256":hashlib.sha256(encoded).hexdigest(),"body_sha256":hashlib.sha256(body_encoded).hexdigest(),"ready":True,"body_ready":sum(r.get("source_body_state")=="CANONICAL" and r.get("index_body_state")=="CANONICAL" for r in rows),"body_blocked":sum(bool(r.get("body_blocked")) for r in rows),"differences":sum(r.get("status")=="差异" for r in rows)}}
