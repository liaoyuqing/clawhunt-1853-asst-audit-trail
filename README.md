# 员工 AI 助手 · 对话留痕（只增不改 / 哈希链 / 保留期清理 / 审计导出）

会员空间平台 Stage v「员工 AI 助手入口」的留痕与审计后端。实现需求 ①②③④ 与全部 4 条验收标准。

- 技术栈：Python 3.11 / FastAPI / SQLAlchemy 2 / Alembic / PostgreSQL 16（生产）/ SQLite（本地与测试）/ Redis（导出队列的预留位）
- 依赖模块：S2-AUDIT（审计日志）、S2-IAM（身份、角色与数据范围）——本仓库自带桩，对接点见「5. 依赖对接点」
- 只用虚构测试数据；仓库内无任何真实个人信息、企业信息或客户名称；所有密钥只从环境变量读

---

## 1. 五分钟跑起来

```bash
# 1) 依赖（Python 3.11+）
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 2) 唯一必填的环境变量：留痕加密密钥（32 字节 base64 / 64 位 hex / 任意口令三种写法任选）
#    Linux/macOS
export ASST_TRAIL_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")
#    Windows PowerShell
#    $env:ASST_TRAIL_KEY = python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"

# 3) 建表 + 装不可变触发器（生产用 Alembic，本地这一条也能建全）
export ASST_DATABASE_URL=sqlite+pysqlite:///./var/asst.db      # 生产换成 postgresql+psycopg://...
python -m alembic upgrade head

# 4) 起服务
python -m uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000/docs 就是全部接口的 Swagger
```

预期输出：`alembic upgrade head` 打印 `Running upgrade -> 0001_initial`；`uvicorn` 打印 `Application startup complete.`；
`curl http://127.0.0.1:8000/healthz` 返回 `{"status":"ok"}`。

### 跑测试

```bash
python -m pytest -q                 # 全量：72 passed
python -m pytest tests/test_trail_immutable.py -v   # AC1
python -m pytest tests/test_hash_chain.py -v        # AC2
python -m pytest tests/test_audit_rbac.py -v        # AC3
python -m pytest tests/test_retention_purge.py -v   # AC4
```

测试不需要外部 PostgreSQL、Redis 或任何网络：数据库用临时文件 SQLite，导出队列用 `inline` 同步跑。
批次结果与逐条明细见 `reports/pytest-full.log`、`reports/ac-mapping.md`。

---

## 2. 交付物与文件对照

| 需求/提案条目 | 落在哪个文件 |
| --- | --- |
| 留痕写入（5 类事件） | `app/services/trail_writer.py`（`record_message` / `record_answer` / `record_tool_call` / `record_blocked_event` / `record_feedback`） |
| 原文加密 + 脱敏文本 | `app/services/crypto.py`（AES-256-GCM 信封）、`app/services/crypto.py`（手机号中 4 位 / 身份证全掩 / 邮箱留 2 位） |
| 只增不改（DB 层） | `app/db/immutability.py`（`BEFORE UPDATE OR DELETE` 触发器 + 两列白名单）、`migrations/versions/0001_initial.py` |
| 删除会话只隐藏 | `app/services/conversations.py::hide_conversation`（只有 `hidden_by_user` 一处写入，无 DELETE） |
| 哈希链计算任务 | `app/services/hash_chain.py`（`build_chain` / `daily_build`）、`app/workers/hash_chain.py` |
| 哈希链校验与断点 | `app/services/hash_chain.py::verify_chain`、`app/workers/verify_chain.py` |
| 保留期清理任务 | `app/services/retention.py`、`app/workers/retention_purge.py` |
| 3 个审计接口 | `app/api/assistant/audit.py`（列表 / 详情 / 导出，另有任务状态与下载两条配套路由） |
| 异步导出 + 10 分钟链接 | `app/services/export.py`（JSONL、HMAC 签名、单次可用） |
| 审计日志（S2-AUDIT 桩） | `app/services/audit_log.py` + `audit_log` 表（同样装了只增不改触发器） |
| 身份与角色（S2-IAM 桩） | `app/services/iam.py` + `app/api/deps.py::require_auditor` |
| 测试 | `tests/`（6 个测试文件，72 条用例） |

---

## 3. 接口一览

### 审计接口（仅审计角色；每次调用都写一条审计日志）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/assistant/audit/conversations` | 列表。筛选：`user_id` / `from` / `to` / `tool_name` / `negative_feedback` / `blocked` / `hidden_by_user`；分页：`page` / `page_size`（上限 200） |
| GET | `/api/assistant/audit/conversations/{id}` | 详情。逐条返回解密后的完整内容、引用文档与版本、模型名、提示词版本、工具调用与确认人、拦截事件、反馈 |
| POST | `/api/assistant/audit/exports` | 异步导出 JSONL，返回 `202 + job_id` |
| GET | `/api/assistant/audit/exports/{job_id}` | 任务状态；`finished` 时返回 `download_url`（10 分钟有效） |
| GET | `/api/assistant/audit/exports/{job_id}/download?token=&sig=` | 凭签名令牌下载（单次可用） |

### 业务侧留痕入口（AI 助手本体属其他工单，这里只做留痕落点）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/assistant/conversations` | 新建会话 |
| DELETE | `/api/assistant/conversations/{id}` | **只置 `hidden_by_user`，返回 204，留痕一行不删** |
| POST | `/api/assistant/conversations/{id}/messages` | 写用户消息留痕（原文加密 + 脱敏文本） |
| POST | `/api/assistant/conversations/{id}/answers` | 写回答留痕（doc_id 与版本、模型名、提示词版本） |
| POST | `/api/assistant/conversations/{id}/tool-calls` | 写工具调用留痕（工具名、版本、脱敏参数、结果摘要、确认人与时间） |
| POST | `/api/assistant/conversations/{id}/blocked-events` | 写关停开关拦截事件 |
| POST | `/api/assistant/conversations/{id}/feedback` | 写用户反馈（1-5 分 + 脱敏评论） |

### 运维接口与定时任务

| 方法 | 路径 / 入口 | 说明 |
| --- | --- | --- |
| POST | `/api/assistant/ops/hash-chain/build` | 计算指定日期（缺省=前一日及所有待建链日期）的哈希链 |
| POST | `/api/assistant/ops/hash-chain/verify` | 从链首重算，返回 `ok` / `first_break_seq` / `expected_hash` / `actual_hash` |
| POST | `/api/assistant/ops/retention/purge` | 执行保留期清理，返回摘要并写审计日志 |
| CLI | `python -m app.workers.hash_chain` | 每日 02:10 由 cron 调起 |
| CLI | `python -m app.workers.verify_chain [YYYY-MM-DD]` | 断点则退出码 2（告警系统可抓） |
| CLI | `python -m app.workers.retention_purge` | 每日清理 |

---

## 4. 环境变量（全部只从环境变量读，仓库内无任何真实密钥）

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ASST_TRAIL_KEY` | **是** | 无 | 留痕原文加密密钥。32 字节 base64 / 64 位 hex / 任意口令（走 HKDF-SHA256 派生）。**缺失时拒绝写入，绝不退化成明文** |
| `ASST_DATABASE_URL` | 否 | `sqlite+pysqlite:///./var/asst.db` | 生产填 `postgresql+psycopg://user@host:5432/db` |
| `ASST_LOG_RETENTION_DAYS` | 否 | `180` | 留痕保留期（天）。<=0 会被拒绝 |
| `ASST_PURGE_BATCH_SIZE` | 否 | `1000` | 每次清理的批量行数（避免长事务） |
| `ASST_EXPORT_DIR` | 否 | `./var/exports` | 导出 JSONL 落盘目录 |
| `ASST_EXPORT_URL_TTL_SECONDS` | 否 | `600` | 下载链接有效期（需求写死 10 分钟） |
| `ASST_EXPORT_MAX_ROWS` | 否 | `100000` | 单文件行数上限 |
| `ASST_EXPORT_QUEUE` | 否 | `thread` | `inline`（同步，测试用）/ `thread`（进程内线程） |
| `ASST_EXPORT_SIGNING_KEY` | 否 | 由 `ASST_TRAIL_KEY` 派生 | 下载链接 HMAC 签名密钥 |
| `ASST_PUBLIC_BASE_URL` | 否 | `http://127.0.0.1:8000` | 下载链接里的对外基址 |
| `ASST_IAM_STUB_TOKENS` | 否 | 空 | S2-IAM 桩的令牌映射（仅本地联调；格式见下） |

`ASST_IAM_STUB_TOKENS` 格式（只放虚构身份）：

```
token:<operator_uuid>:<org_uuid>:<role,role>:<site_uuid|site_uuid>;token2:...
```

---

## 5. 依赖对接点（S2-AUDIT / S2-IAM）

### 5.1 S2-AUDIT（审计日志）

**桩在哪里**：`app/services/audit_log.py`，落 `audit_log` 表（已装只增不改触发器）。

**真实对接点**：把 `write_audit_log(session, event)` 换成 S2-AUDIT 的写入调用；调用方（`app/api/assistant/audit.py`、`app/services/hash_chain.py`、`app/services/retention.py`）无需改动。

| 本单字段 | S2-AUDIT 对应 | 说明 |
| --- | --- | --- |
| `org_id` | 组织 id | 多租户隔离键 |
| `action` | 事件类型 | `audit_view` / `audit_export` / `export_download` / `hash_chain` / `purge` |
| `target_type` + `target_id` | 对象引用 | 会话、导出任务、链（`org:date`）、组织 |
| `operator_id` + `operator_role` | 操作人 | 系统任务写 `operator_role=system` |
| `ip` | 来源 IP | 取 `X-Forwarded-For` 首段，回落直连地址 |
| `detail` | 结构化载荷 | 查看写筛选条件与条数；导出写筛选与格式；清理写 `count` / `range_from` / `range_to` / `chain_tail_hash`；链写 `chain_date` / `row_count` / `tail_hash` |

### 5.2 S2-IAM（身份、角色与数据范围）

**桩在哪里**：`app/services/iam.py::StubIamClient`，用环境变量把 Bearer 令牌映射成 `Principal(operator_id, org_id, roles, site_ids)`。

**真实对接的三个点**：

1. `resolve_principal(bearer_token)` → token introspection，返回 `operator_id` / `org_id` / `roles` / `site_ids`。当前实现在 `app/api/deps.py::get_principal` 中调用，**只认 `Authorization: Bearer <token>`**（`X-Api-Key` 之类一律不认）。
2. `assert_audit_access(principal)` → 审计角色判定。当前要求 `roles ∩ {auditor} ≠ ∅`，否则 403 `FORBIDDEN_AUDIT_ROLE`。若 S2-IAM 的角色名不同（如 `audit_admin`），改 `app/services/iam.py::AUDIT_ROLES` 一处即可。
3. `site_ids` → 数据范围。非空时列表按 site 收窄、详情与导出越界一律 404（不用 403，避免成为枚举探针）。

---

## 6. 四条验收标准怎么验

### AC1 用户删除会话后，审计接口仍能查到该会话完整内容

```bash
python -m pytest tests/test_trail_immutable.py::test_hidden_but_queryable -v
```

手工复现：① `POST /api/assistant/conversations` 建会话 → ② 交替调 `/messages` 与 `/answers` 写 3 轮 → ③ 记下 `GET /api/assistant/audit/conversations/{id}` 的 `trail_count` → ④ `DELETE /api/assistant/conversations/{id}`（204）→ ⑤ 再查同一个详情：200，`hidden_by_user=true`，`trail_count` 不变，每条消息解密后与原文逐字一致且 `sha256(content) == content_sha256`。
配套用例：`test_delete_does_not_remove_rows`（DELETE 前后 `SELECT count(*)` 相等）、`test_encrypted_at_rest`（库里是 `enc:v1:` 密文）、`test_db_trigger_blocks_update` / `test_db_trigger_blocks_delete_without_purge_window` / `test_hidden_flag_only_whitelist`（触发器兜底）。

### AC2 篡改一条历史留痕后，哈希链校验任务报告断点位置

```bash
python -m pytest tests/test_hash_chain.py::test_tamper_detected -v
```

用例流程：写 50 条留痕 → `build_chain` 建链（`ok=true`）→ 卸触发器改第 23 条 `payload`（模拟超级用户绕过）→ `verify_chain` 返回 `ok=false`、`first_break_seq=23`、`break_reason=payload_changed`、`expected_hash` 与 `actual_hash` 均非空且不相等。
同类证据：`test_chain_tail_written_to_audit`（链尾 + 条数进 `audit_log`）、`test_rerun_idempotent`（同日重跑不产生第二条链）、`test_rerun_after_tamper_does_not_whiten`（重跑不洗白断点）、`test_append_after_break`、`test_chain_entry_tamper_detected`、`test_missing_trail_without_marker_detected`、`test_multi_day_chains_link`、`test_row_hash_formula_matches_spec`。

### AC3 非审计角色 403；审计角色每次查看产生一条审计记录

```bash
python -m pytest tests/test_audit_rbac.py -v
```

手工复现：① 用员工令牌 `GET /api/assistant/audit/conversations` → 403 `FORBIDDEN_AUDIT_ROLE`（列表 / 详情 / 导出三条路径都有用例）→ ② 换成审计令牌连查 3 次 → 200，且 `audit_log` 里恰好新增 3 条 `action=audit_view`（`operator_id` / `operator_role` / `ip` / `detail.filters` 齐全）→ ③ `POST /audit/exports` 另写一条 `action=audit_export`。
配套用例：`test_export_also_logged`、`test_cross_org_denied_404`、`test_data_scope_filter`、`test_x_api_key_header_not_accepted`、`test_filters_cover_all_dimensions`、`test_pagination`。

### AC4 到期数据清理后，审计日志中有删除摘要

```bash
python -m pytest tests/test_retention_purge.py::test_purge_deletes_expired_and_keeps_recent -v
```

用例流程：造 200 天前 30 条 + 当天 10 条 → `run_purge`（默认 180 天）→ 物理删 30 条、留 10 条、`SELECT count(*) WHERE occurred_at < cutoff` 为 0 → `audit_log` 有 `action=purge`，`detail` 含 `count=30` / `retention_days=180` / `range_from` / `range_to` / `chain_tail_hash`。
配套用例：`test_retention_env_respected`（改 30 天）、`test_purge_is_batched`（25 条 / 批 10 = 3 批）、`test_hidden_rows_are_purged_too`（隐藏不豁免）、`test_chain_survives_purge`（清理后链校验不误报）、`test_purge_window_closes_after_run`、`test_dry_run_does_not_delete`、`test_invalid_retention_rejected`、`test_purge_all_orgs_iterates`。

### 附加证据：进程级端到端取证（真实 uvicorn + 真实 HTTP）

pytest 用的是进程内 ASGI 调用（TestClient）。为了证明「真的能起服务、真的走 socket」，另有一份独立取证脚本：

```bash
python scripts/e2e_evidence.py     # 末行打印 EXIT=0 才算全绿
```

它会：① 用 `alembic upgrade head` 建库；② 起真实 `uvicorn` 进程；③ 用 `httpx` 逐条打上面 4 条 AC 与导出链路；④ 把每一次请求/响应原文写进 `reports/e2e-evidence.md`（含服务端 uvicorn 访问日志）。
脚本自带断言（状态码与业务字段），任何一条不符即 `EXIT=1` 并在报告末尾追加异常小节，不会伪造输出。

---

## 7. 关键口径（写死在代码里，不是口头约定）

**只增不改**

- 应用层：`app/services/` 下没有任何 `DELETE FROM asst_trail` 或改写留痕列的代码路径；会话删除只置 `hidden_by_user`。
- DB 层：`asst_trail` 上 `BEFORE UPDATE OF <除白名单外全部列> ... RAISE(ABORT, 'TRAIL_IMMUTABLE: ...')`；`BEFORE DELETE ... WHEN NOT (清理窗口 AND OLD.occurred_at < cutoff)`。
- 白名单只有两列：`hidden_by_user`（布尔，附带类型校验）与 `purged_at`。列清单由 SQLAlchemy 元数据现算，不会与模型漂移。
- `asst_hash_chain` 与 `audit_log` 连白名单都没有：任何 UPDATE/DELETE 都拒（链记录允许在清理窗口内删除，否则清理到不了链）。
- 物理删除的唯一入口是 `app/services/retention.py::run_purge`：它在同一事务里打开「清理窗口」（写入 cutoff）→ 删除 → 关闭窗口。窗口关闭后 DELETE 立即恢复全拒。

**哈希链**

```
row_hash = sha256(prev_hash || canonical_json(payload))
```

- 链按 `(org_id, chain_date)` 分链；链首 `prev_hash` 是 64 个 0（十六进制串），除非同 org 有更早的链——那时接在更早链的链尾之后，跨日因此也是连续的一条大链。
- `canonical_json`：键名排序、`separators=(",", ":")`、`ensure_ascii=False`、UTC 秒级 ISO（`2026-01-02T03:04:05Z`）、浮点转最短往返字符串、UUID 小写。
- 每天对前一日留痕计算；链尾哈希与条数写 `audit_log`（`action=hash_chain`）。
- 幂等：同 `(org, date)` 只有一条链（唯一约束 `uq_asst_hash_chain_org_date_seq`）与一条执行记录；重跑只追加新行，**绝不重算历史行**（否则重跑就等于给篡改者洗白）。
- 断点报告：`first_break_seq` + `break_reason`（`payload_changed` / `chain_entry_changed` / `prev_hash_changed` / `seq_gap` / `tail_changed` / `trail_row_missing`）+ `expected_hash` / `actual_hash`。
- 链记录自带 `payload_canonical`：校验不依赖业务行是否已被清理。

**保留期清理**

- 保留期 = `ASST_LOG_RETENTION_DAYS`（默认 180）；清理条件 `occurred_at < now - retention`，与是否隐藏无关。
- 按 `ASST_PURGE_BATCH_SIZE`（默认 1000）分批，每批一个事务内完成。
- 每批被删的留痕连同其链记录一起删，并写 `asst_purge_marker`（含被删段链尾哈希）。校验据此把「到期删除」与「篡改」区分开：标记覆盖到的缺失行跳过，没标记的缺失行报 `trail_row_missing`。
- 每次清理写一条 `audit_log`（`action=purge`）：`count` / `retention_days` / `cutoff` / `range_from` / `range_to` / `chain_tail_hash` / `chain_entries_deleted` / `batches`。

**加密与脱敏**

- 原文：AES-256-GCM，AAD 绑定留痕行 id（密文被搬到别的行会解密失败），信封格式 `enc:v1:<base64(nonce12 || ciphertext || tag16)>`。
- 脱敏：手机号保前 3 后 4（`138****5678`）、身份证保前 2 其余全掩、邮箱本地部分保前 2（`zh***@example.com`）、16-19 位卡号保前 4 后 4；凭据类键名（password/secret/token/api_key…）整值掩成 `***`。
- 脱敏文本与密文同表落库：列表与导出默认给脱敏文本，详情接口才解密。

**导出**

- JSONL：首行 `type=manifest`（任务 id、org、筛选条件、生成时间、schema 版本），其余每行 `type=trail`，含 `seq` / `trail_id` / `kind` / `occurred_at` / `content`（解密后原文）/ `content_redacted` / `content_sha256` / `payload`。
- 文件 sha256 落 `asst_export_job.file_sha256`，用于事后核对文件没被换过。
- 下载链接：HMAC-SHA256 签名 + 库内只存令牌 sha256，10 分钟有效、单次可用（第二次 410）。
- 导出动作本身写 `audit_log`（`action=audit_export`），下载写 `action=export_download`。

---

## 8. 验证边界（如实说明：哪些在本机跑不了、用什么替代）

本仓库的自动化测试全部在 **SQLite** 上跑通（72 条），**没有在本机连上真实 PostgreSQL 16**（本机无 PG 实例、无 Docker）。因此下面这些差异必须在集成环境复核：

| 项 | 测试用的 SQLite | 生产 PG16 | 影响与补救 |
| --- | --- | --- | --- |
| 不可变触发器实现 | 列清单版 `BEFORE UPDATE OF <cols>` + `RAISE(ABORT)` | plpgsql `BEFORE UPDATE OR DELETE` + `to_jsonb(OLD) - 白名单` 差集 + `RAISE EXCEPTION ... ERRCODE P0001` | 两套 DDL 都写在 `app/db/immutability.py`，Alembic 迁移按方言分支；PG 版靠 `IS DISTINCT FROM` 判差集，语义等价但**未在真实 PG16 上执行过**，CI 首跑请确认 `CREATE OR REPLACE FUNCTION` 与触发器创建成功 |
| 并发 | 单连接串行；`AsstTrailCounter` 用 `UPDATE ... RETURNING` 自增 | 多连接并发 | SQLite 下并发退化为文件锁；PG 下 `UPDATE ... RETURNING` 是行级原子操作。**高并发下 seq 连续性未做压测** |
| JSON 列 | `TEXT` 存 JSON，`json_set()` 用于模拟篡改 | `jsonb` | PG 上 payload 是 `jsonb`，键序被归一；`canonical_json` 已排序所以哈希口径不受影响，但**PG 上未实测哈希一致性** |
| UUID | 存 32 位 hex 文本 | 原生 `uuid` | 参数绑定与索引行为不同，未在 PG 上压测 |
| 导出队列 | `inline` 同步执行 + `thread` 线程模式 | 建议 Redis 队列（`requirements.txt` 的 `redis` 预留） | 多副本部署时 `thread` 模式的任务只在单进程内跑，需换成共享队列；当前**没有交付 Redis 队列实现** |
| 导出存储 | 本地磁盘 `ASST_EXPORT_DIR` | 对象存储（MinIO/S3） | `app/services/export.py::export_dir()` 是唯一落盘点，替换成对象存储只需改这一处 |
| S2-AUDIT / S2-IAM | 本仓库桩（`audit_log` 表 + 环境变量令牌映射） | 真实模块 | 对接点见第 5 节；桩的行为（403 码、字段名）按需求文字实现，**未与真实 S2 模块联调** |
| 定时任务 | 直接 `python -m app.workers.*` | cron / Airflow | 入口脚本已备，**未在调度系统里注册** |
| 加密密钥管理 | 环境变量明文注入 | 建议 KMS / Vault | 按需求「配置与密钥只从环境变量读取」实现；KMS 集成不在本单范围（提案 ⑦ 已写明不含） |

**明确没有做的**（与提案 ⑦「不含」一致）：助手对话功能本身、审计工作台前端、SIEM/外部日志平台对接、KMS 集成、历史数据迁移。

**另外两处需要说明的取舍**：

1. `asst_trail.updated_at` 与 `created_at` 恒等且**不设 `onupdate`**。留痕行只增不改，`updated_at` 若随 ORM 更新自动写，就会让「合法改 `hidden_by_user`」也带上一个白名单外的列而被触发器拒掉。业务表统一含该列的约定保留，语义按「留痕行不更新」解释。
2. 哈希链在清理后**不能逐条复核已删除段**：段内每条记录的 `row_hash` 随行删除，只保留段尾哈希与条数（`asst_purge_marker`）。因此「清理后的链完整性」证明的是「未删除段未被改」，历史段只保留到段尾哈希这一层。若需逐条永久可复核，需把链记录独立归档到审计日志模块（本单未做，见提案 ⑥「需确认②」）。

---

## 9. 目录结构

```
app/
  config.py                       环境变量读取（唯一配置入口）
  core/canonical.py               确定性 JSON（哈希链输入）
  db/base.py                      声明基类与业务表通用列
  db/session.py                   引擎 / 会话工厂 / 建表
  db/immutability.py              只增不改触发器（PG16 + SQLite 两套 DDL）
  models/asst_trail.py            留痕模型（含 purge_marker / 计数器）
  models/asst_hash_chain.py       链记录与链执行记录
  models/asst_export_job.py       导出任务与下载令牌
  models/asst_conversation.py     会话（hidden_by_user）
  models/audit_log.py             审计日志（S2-AUDIT 桩的落库）
  services/crypto.py              AES-256-GCM 信封加解密 + 密钥派生 + 脱敏规则
  services/trail_writer.py        5 类留痕写入
  services/conversations.py       新建会话、删除会话（只隐藏）
  services/hash_chain.py          链计算与校验
  services/retention.py           保留期清理
  services/query.py               审计查询与筛选
  services/export.py              JSONL 导出与下载令牌
  services/audit_log.py           S2-AUDIT 桩
  services/iam.py                 S2-IAM 桩
  api/deps.py                     依赖注入与角色闸门
  api/assistant/audit.py          3 个审计接口 + 任务状态 + 下载
  api/assistant/conversations.py  业务侧留痕入口
  api/assistant/ops.py            运维接口
  main.py                         FastAPI 装配
  workers/hash_chain.py           每日链计算任务
  workers/verify_chain.py         链校验任务（断点则退出码 2）
  workers/retention_purge.py      保留期清理任务
migrations/                       Alembic 迁移（0001_initial）
tests/                            6 个测试文件 / 72 条用例
reports/                          pytest 原始输出与 AC 对照表
```

---

## 10. 常见问题

**`TrailKeyMissing: 缺少环境变量 ASST_TRAIL_KEY`** —— 没设密钥。这是设计如此：留痕必须加密，宁可不写也不落明文。
**审计接口 401 而不是 403** —— 令牌没被识别。检查 `Authorization` 头是否为 `Bearer <token>`（不是 `X-Api-Key`），以及 `ASST_IAM_STUB_TOKENS` 里有没有这个令牌。
**审计接口 403 `FORBIDDEN_AUDIT_ROLE`** —— 身份有效但角色不含 `auditor`。
**删除留痕报 `TRAIL_IMMUTABLE`** —— 预期行为。物理删除只能由 `run_purge` 在清理窗口内做。
**链校验报 `no_chain`** —— 该日期还没建链，先跑 `build_chain` 或 `hash_chain_job`。
**清理后链校验报断点** —— 先看是否缺 `asst_purge_marker`：直接删库不写标记会被正确识别成破坏。