import csv,io,json
def build_archive(run):
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","body_state","canonical_source_body","canonical_index_body","evidence"])
    meta=run["manifest"]
    for row in run["stages"][-1]["rows"]:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),
                         ("阻断:"+row["block_reason"]) if row.get("body_blocked") else (row.get("source_body_state","")),
                         json.dumps(row.get("source_canonical_body"),ensure_ascii=False),
                         json.dumps(row.get("index_canonical_body"),ensure_ascii=False),
                         json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),"","","",json.dumps(row,ensure_ascii=False)])
    return "﻿"+stream.getvalue()
