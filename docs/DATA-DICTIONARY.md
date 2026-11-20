# 双写定义

event_id唯一，doc_id文本保留。side为source或index；版本正整数按Decimal比较并作为文本序列化。upsert要求JSON对象并作为整篇正文快照（正文基线），patch要求JSON对象且只携带本次改动字段（字段级修订），delete忽略payload并产生墓碑。patch中字段值为JSON null表示字段墓碑，重放时从正文移除该字段；未在patch中提及的字段保持不变。相同文档同端同版本不同内容全部隔离，存在未裁决冲突的主体不回落旧版本；同内容合并来源；较高版本覆盖低版本，迟到旧写不得覆盖墓碑。

## 字段级修订正文裁决（唯一可检索正文）

每个 (doc_id, side) 主体能否形成唯一可检索正文，按下列顺序裁决，结果与事件到达顺序无关：

1. 同版冲突：同 (doc_id, side, version) 出现不同内容，整主体隔离（SAME_VERSION_CONFLICT），不形成新正文、不回落旧版本；同内容则合并来源。
2. 基线：取版本链上最高版本的整篇 upsert 作为可追溯基线；链上只有 patch 而无 upsert 时阻断（NO_BASELINE），保留原状态。纯 delete 链沿用墓碑语义。
3. 连续性：基线版本到链首版本之间缺任一整数版本即阻断（VERSION_GAP），冻结基线上一可用正文，不把任何补丁盖上。
4. 重放：从基线按版本号升序重放链上全部事件——upsert 整体替换、patch 仅改提及字段（null 撤字段）、delete 置墓碑。新的 upsert 重新建立基线，被 delete 的旧正文不复活；delete 之后若只有 patch 而无新 upsert，按 NO_BASELINE 阻断，禁止字段补丁让已删文档复活。

阻断（BLOCKED）时 canonical_body 仅在 VERSION_GAP 场景冻结为基线正文用于追溯，body_state 仍为 BLOCKED，不产生新的可检索正文；block_reason 与 block_detail 随主体透传到页面、差异定责与归档。M4 以源裁决正文为基准做字段级定责：index_extra_fields（索引多出，含已撤字段回流）、index_missing_fields（索引缺失）、index_changed_fields（取值改动）。M5 对阻断主体给“正文阻断”门禁且 reported=0，与延迟、缺失分开。M7 封存 body_sha256，页面、差异、归档共用 M3 同一份 canonical_body，归档侧不二次重建。

双端最终版本、操作、文档分别核验。目标缺失、删除未确认、索引领先、陈旧索引、内容差异分开展示。source_time与visible_time带偏移；可见延迟按实际秒数。默认阈值120秒，等于阈值通过。已可见但超过阈值为延迟，尚无目标且等待时间超过阈值为缺失，等待中不冒充延迟。

对账expected=每文档应通过的1，reported=门禁通过1或未通过0，逐条保存visibility_state和gate原因，允许按文档复核。

业务ID和超大整数按文本保形；Decimal参与数量金额计算，对外序列化为字符串。哨兵集合定义在app/values.py，缺失值不能当零。所有时间带UTC偏移，先转UTC比较，业务日按指定地区划分。区间统一[start,end)。重复业务键保留第一条、完全相同行计重复；值冲突行隔离且保留原值。空文件、全空白、仅表头分开拒绝。

运行清单包括输入sha256、规则版本、参数、生成时刻与耗时。源行编号以CSV解析器的物理结束行号表示，引号内换行不增加记录数。输入超过4MB返回413，非法表头和类型返回422，失效版本复核返回409。