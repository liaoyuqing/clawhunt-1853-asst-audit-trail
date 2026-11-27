# 验收标准对照表（AC ↔ 用例 ↔ 证据）

全部数据由 `python -m pytest -v` 真实产生，原始日志见同目录 `pytest-*.log`。
汇总：**70 passed in 27.60s**（`pytest-full.log` 末行）。

本次运行环境：Windows / Python 3.11.9 / pytest 9.2.0 / SQLite（临时文件库）；无 Docker、无 PostgreSQL 实例，
因此未跑真实 PG16 —— 差异与补救见 README 第 8 节「验证边界」。

---

## AC1 用户删除会话后，审计接口仍能查到该会话完整内容（测试）

| 用例 | 验证内容 | 日志 |
| --- | --- | --- |
| `test_hidden_but_queryable` | 主用例：3 轮问答 → DELETE 会话（204）→ 审计详情 200，`hidden_by_user=true`，`trail_count` 保持 6，每条消息解密后与原文逐字一致且 `sha256(content)==content_sha256` | `pytest-ac1-immutable.log` |
| `test_delete_does_not_remove_rows` | DELETE 前后 `SELECT count(*) FROM asst_trail` 相等（5 → 5） | 同上 |
| `test_encrypted_at_rest` | 库内 `content_enc` 以 `enc:v1:` 开头、不含原文；`content_redacted` 里 `13812345678` 已掩成 `138****5678` | 同上 |
| `test_db_trigger_blocks_update` | 直接 `UPDATE asst_trail SET content_redacted=...` → DB 抛 `TRAIL_IMMUTABLE` | 同上 |
| `test_db_trigger_blocks_delete_without_purge_window` | 无清理窗口时 `DELETE FROM asst_trail` → `TRAIL_IMMUTABLE` | 同上 |
| `test_hidden_flag_only_whitelist` | 白名单列 `hidden_by_user=1` 可改；`hidden_by_user=7`（非布尔）被拒 | 同上 |
| `test_audit_list_shows_hidden_conversation` | 列表接口也能查到已隐藏会话 | 同上 |
| `test_unknown_conversation_404` | 不存在的会话 → 404 | 同上 |

AC1 对应文件：`app/services/conversations.py`（`hide_conversation` 只写 `hidden_by_user`）、`app/api/audit.py`（详情解密）、`app/db/immutability.py`（触发器）。

---

## AC2 篡改一条历史留痕后，哈希链校验任务报告断点位置（测试用例）

| 用例 | 验证内容 | 日志 |
| --- | --- | --- |
| `test_tamper_detected` | 主用例：50 条留痕建链 → 卸触发器改第 23 条 `payload` → 校验 `ok=false`、`first_break_seq=23`、`break_reason=payload_changed`、`expected_hash != actual_hash`，两者均 64 位 | `pytest-ac2-hash-chain.log` |
| `test_tamper_reported_by_http_verify_endpoint` | 同一断点经 `POST /api/assistant/ops/hash-chain/verify` 也能取到 | 同上 |
| `test_chain_tail_written_to_audit` | 链尾哈希 + 条数 + 链首写入 `audit_log`（`action=hash_chain`） | 同上 |
| `test_first_break_is_genesis_for_single_day` | 单日链链首 `prev_hash` 为 64 个 0 | 同上 |
| `test_rerun_idempotent` | 同日重跑：链记录数仍 10、执行记录仍 1、链尾不变 | 同上 |
| `test_rerun_after_tamper_does_not_whiten` | 篡改后重跑只追加新行，断点仍在（`first_break_seq=2`） | 同上 |
| `test_append_after_break` | 断点之后仍能继续追加留痕 | 同上 |
| `test_chain_entry_tamper_detected` | 不改业务行、只改链记录 → 报 `payload_changed` / `chain_entry_changed`，`first_break_seq=4` | 同上 |
| `test_missing_trail_without_marker_detected` | 无清理标记的缺失行 → `trail_row_missing`，`first_break_seq=2` | 同上 |
| `test_multi_day_chains_link` | 跨日链：后一日链首 `prev_hash` == 前一日链尾哈希 | 同上 |
| `test_daily_build_covers_pending_dates` | 每日任务为所有待建链日期补链 | 同上 |
| `test_row_hash_formula_matches_spec` | 逐字节验证 `row_hash == sha256(prev_hash \|\| payload_canonical)` | 同上 |
| `test_chain_builds_and_verifies_clean` | 未篡改时 `ok=true`、`checked_rows=5` | 同上 |

AC2 对应文件：`app/services/hash_chain.py`（`build_chain` / `verify_chain` / `daily_build`）、`app/core/canonical.py`、`app/workers/hash_chain.py`、`app/workers/verify_chain.py`。

---

## AC3 非审计角色调用审计接口返回 403；审计角色每次查看产生一条审计记录

| 用例 | 验证内容 | 日志 |
| --- | --- | --- |
| `test_employee_list_forbidden` | 员工令牌查列表 → 403 `FORBIDDEN_AUDIT_ROLE` | `pytest-ac3-rbac.log` |
| `test_employee_detail_forbidden` | 员工令牌查详情 → 403 | 同上 |
| `test_employee_export_forbidden` | 员工令牌建导出 → 403 | 同上 |
| `test_auditor_list_writes_one_audit_record` | 审计令牌查列表 → 恰好多 1 条 `audit_view`（含 `operator_id` / `operator_role=auditor` / `ip` / `detail.filters` / `detail.total`） | 同上 |
| `test_auditor_detail_writes_audit_record` | 查详情 → 多 1 条，`target_id` 为会话 id，`detail.trail_count=5` | 同上 |
| `test_every_view_writes_one_record` | 连查 3 次 → 恰好 3 条（每次查看一条） | 同上 |
| `test_export_also_logged` | 导出另写 `audit_export`，`target_id=job_id`，`detail.filters.blocked=true` | 同上 |
| `test_unauthenticated_401` | 无头 / 错令牌 → 401 | 同上 |
| `test_x_api_key_header_not_accepted` | 只发 `X-Api-Key` → 401（只认 Bearer） | 同上 |
| `test_cross_org_denied_404` | 跨 org → 404（不 403，防枚举） | 同上 |
| `test_data_scope_filter` | site 范围外会话不出现在列表、详情 404 | 同上 |
| `test_filters_cover_all_dimensions` | 用户 / 时间 / 工具名 / 差评 / 被拦截五维筛选逐一命中 | 同上 |
| `test_pagination` | 3 条 / 页 2 → 2 页、无重复 | 同上 |
| `test_invalid_time_window_400` | `from > to` → 400 `INVALID_TIME_WINDOW` | 同上 |

AC3 对应文件：`app/api/deps.py`（`require_auditor`）、`app/services/iam.py`、`app/services/audit_log.py`、`app/api/audit.py`。

---

## AC4 到期数据清理后，审计日志中有删除摘要（测试）

| 用例 | 验证内容 | 日志 |
| --- | --- | --- |
| `test_purge_deletes_expired_and_keeps_recent` | 主用例：200 天前 30 条 + 当天 10 条 → 删 30 留 10；`occurred_at < cutoff` 余 0；`audit_log` 有 `action=purge`，`detail` 含 `count=30` / `retention_days=180` / `range_from` / `range_to` / `chain_tail_hash` | `pytest-ac4-retention.log` |
| `test_default_retention_days_env` | 默认 180；`ASST_LOG_RETENTION_DAYS=30` 生效；cutoff 计算正确 | 同上 |
| `test_retention_env_respected` | 改 30 天：45 天前 5 条被清、10 天前 4 条保留 | 同上 |
| `test_purge_is_batched` | 25 条 / 批 10 → 3 批，全删 | 同上 |
| `test_hidden_rows_are_purged_too` | `hidden_by_user=1` 的 6 条到期同样清理（隐藏不豁免） | 同上 |
| `test_chain_survives_purge` | 清理后链校验 `ok=true`（写入 1 条 `asst_purge_marker` 衔接） | 同上 |
| `test_purge_window_closes_after_run` | 清理结束后 `DELETE FROM asst_trail` 恢复全拒 | 同上 |
| `test_dry_run_does_not_delete` | `dry_run=True` 只统计（3 条），库不变、不写审计 | 同上 |
| `test_purge_endpoint_writes_summary` | 运维接口触发清理：`deleted_rows=7`，审计 1 条 | 同上 |
| `test_invalid_retention_rejected` | `retention_days=0` → 400 `INVALID_RETENTION` | 同上 |
| `test_purge_all_orgs_iterates` | 定时任务入口遍历组织，每组织一条审计 | 同上 |

AC4 对应文件：`app/services/retention.py`、`app/db/immutability.py`（清理窗口 `asst_purge_guard`）、`app/workers/retention_purge.py`。

---

## 提案承诺的其余项（对应测试）

| 提案条目 | 用例 | 日志 |
| --- | --- | --- |
| 异步导出 202 + job_id | `test_export_returns_202_with_job_id` | `pytest-export.log` |
| 10 分钟下载链接 | `test_export_status_offers_download_link`、`test_download_expires_after_ten_minutes` | 同上 |
| 单次可用 | `test_download_single_use` | 同上 |
| 签名防伪 | `test_download_rejects_tampered_signature` | 同上 |
| JSONL 每行可复核 | `test_download_streams_jsonl` | 同上 |
| 导出复现筛选口径 | `test_export_honours_filters` | 同上 |
| 文件指纹 | `test_export_file_has_sha256` | 同上 |
| 下载写审计 | `test_download_writes_audit_record` | 同上 |
| 跨 org 隔离 / 文件独立 | `test_export_job_isolated_per_org`、`test_exports_are_separate_files` | 同上 |
| 加密与脱敏口径 | `pytest tests/test_crypto_redaction.py`（13 条） | `pytest-crypto-redaction.log` |
