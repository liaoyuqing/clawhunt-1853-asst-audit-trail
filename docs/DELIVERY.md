# 员工 AI 助手 · 对话留痕（只增不改 / 哈希链 / 保留期清理 / 审计导出）— 交付说明

仓库（公开，可匿名克隆）：https://github.com/liaoyuqing/clawhunt-1853-asst-audit-trail
HEAD commit：包含本文件的最新提交（仓库默认分支 main；提交回执里的 sha 即此提交）
本单交付物 = 该仓库全部内容（63 个受版本控制的文件，其中 `docs/DELIVERY.md` 即本交付说明的仓库副本，仅 HEAD 行随提交更新），正文所有路径都能在仓库里逐条打开。

## 一、依赖与运行环境

依赖：Python 3.11+（实测 3.11.9）、FastAPI 0.136.1、SQLAlchemy 2.0.49、Alembic 1.18.4、Pydantic 2.14.4、cryptography 50.0.2、psycopg[binary] 3.2.3、httpx 0.28.1、pytest 9.2.0、freezegun 1.5.5（见 requirements.txt）。生产目标库为 PostgreSQL 16，本仓库自带 PG 方言的触发器 DDL 与 Alembic 迁移。
不需要：Redis、对象存储、任何外部 SaaS、任何真实客户或会员数据（全部为虚构数据），也不需要联网。测试用临时文件 SQLite，导出队列用 inline 同步模式，因此拿到仓库后不需要起任何外部服务就能跑完全部测试。
起服务（三条命令）：
```
pip install -r requirements.txt
export ASST_TRAIL_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")   # 唯一必填变量，32 字节 base64 / 64 位 hex / 任意口令三种写法均可
alembic upgrade head && uvicorn app.main:app --port 8000     # 打开 http://127.0.0.1:8000/docs 即全部接口
```
跑测试：`python -m pytest`（末行应为 `72 passed`）。
环境变量清单见 README 第 4 节：ASST_TRAIL_KEY（必填）、ASST_DATABASE_URL、ASST_LOG_RETENTION_DAYS（默认 180）、ASST_PURGE_BATCH_SIZE、ASST_EXPORT_DIR、ASST_EXPORT_URL_TTL_SECONDS（默认 600 秒）、ASST_EXPORT_MAX_ROWS、ASST_EXPORT_QUEUE、ASST_EXPORT_SIGNING_KEY、ASST_PUBLIC_BASE_URL、ASST_IAM_STUB_TOKENS。配置与密钥只从环境变量读，仓库内无任何真实密钥（已对全仓库 git grep 扫描常见密钥前缀，命中 0 条）。

S2-AUDIT / S2-IAM 是桩：审计日志落本地 `audit_log` 表（同样装了只增不改触发器），身份用 `ASST_IAM_STUB_TOKENS` 令牌→(operator, org, roles, sites) 映射。真实替换点只有三个函数，README 第 5 节给了字段对照表：`app/services/audit_log.py::write_audit_log`、`app/services/iam.py::StubIamClient.resolve_principal`、`app/api/deps.py::get_iam`。

## 二、逐条复现步骤（每条 AC：路由/参数 → 操作 → 应看到什么 → 证据文件）

### AC1 用户删除会话后，审计接口仍能查到该会话完整内容
① 路由：`POST /api/assistant/conversations` 建会话 → `POST /api/assistant/conversations/{id}/{messages|answers|tool-calls|blocked-events|feedback}` 写 5 类留痕；② 操作：先 `GET /api/assistant/audit/conversations/{id}`（审计令牌）记下 trail_count，再 `DELETE /api/assistant/conversations/{id}`（员工令牌，返回 204，只置 hidden_by_user，库里一行不删），再查同一详情；③ 应看到：详情仍 200、`hidden_by_user=true`、trail_count 不变、每条消息解密后与原文逐字一致且 `sha256(content)==content_sha256`，脱敏副本里手机号是 `138****5678`；④ 证据：`reports/e2e-evidence.md` 的「AC1」小节（真实 uvicorn + httpx，1-1…1-9 共 9 次请求原文）与 `reports/pytest-ac1-immutable.log`（9 条用例）。

### AC2 篡改一条历史留痕后，哈希链校验任务报告断点位置
① 路由：`POST /api/assistant/ops/hash-chain/build {"chain_date":"YYYY-MM-DD"}` → `POST /api/assistant/ops/hash-chain/verify`，同参；② 操作：建链后校验一次（应 ok），再用 sqlite3 卸掉 `trg_asst_trail_no_update` 并 `UPDATE asst_trail SET payload=json_set(payload,'$.content_redacted','TAMPERED-BY-E2E') WHERE seq=8`（模拟数据库超级用户绕过应用层，这是需求里「发现篡改」的真实威胁模型），再校验；③ 应看到：`ok=false`、`first_break_seq=3`、`break_reason=payload_changed`、`expected_hash` 与 `actual_hash` 均 64 位且不等；④ 证据：`reports/e2e-evidence.md`「AC2」小节（2-1/2-2/2-3 三次响应原文）与 `reports/pytest-ac2-hash-chain.log`（13 条用例，含 50 条留痕篡改第 23 条的 `first_break_seq=23` 主用例、链记录被改、缺失行、跨日链、幂等重跑不洗白断点）。

### AC3 非审计角色调用审计接口返回 403；审计角色每次查看产生一条审计记录
① 路由：`GET /api/assistant/audit/conversations`、`GET /api/assistant/audit/conversations/{id}`、`POST /api/assistant/audit/exports`；② 操作：员工令牌依次打三条（403），无令牌与只给 `X-Api-Key` 各打一次（401），再换审计令牌连查列表 3 次；③ 应看到：三条路径 403 `FORBIDDEN_AUDIT_ROLE`、401、401（只认 `Authorization: Bearer`），审计令牌 3 次都 200，且 `audit_log` 里 `action=audit_view` 恰好新增 3 条（e2e 报告里有「查看前 `audit_view` = 2 → 连查 3 次后 = 5，增量 3」的实测核对）；④ 证据：`reports/e2e-evidence.md`「AC3」小节 + `reports/pytest-ac3-rbac.log`（14 条用例，含每次查看一条、导出另写一条、跨 org 404 防枚举、site 数据范围）。

### AC4 到期数据清理后，审计日志中有删除摘要
① 路由：`POST /api/assistant/ops/retention/purge {}`（默认取 `ASST_LOG_RETENTION_DAYS=180`）；② 操作：写 4 条 200 天前的留痕 + 近期留痕，触发清理；③ 应看到：`deleted_rows=4`、`cutoff` 约等于今天减 180 天、`range_from/range_to` 为被删数据的时间范围，且 `audit_log` 里 `action=purge` 的 `detail` 含 `count / retention_days=180 / cutoff / range_from / range_to / chain_tail_hash / chain_entries_deleted / batches`；清理后近期留痕的审计详情仍 200；④ 证据：`reports/e2e-evidence.md`「AC4」小节（含 audit_log 删除摘要的原始 JSON）与 `reports/pytest-ac4-retention.log`（11 条用例：改 30 天配置生效、分批、隐藏行不豁免、清理后链校验不误报、清理窗口关闭后 DELETE 恢复全拒、dry_run 不删、0 天非法参数 400、多组织遍历）。

### 附加（提案承诺的导出链路）
`POST /api/assistant/audit/exports` → 202 + job_id；`GET /api/assistant/audit/exports/{job_id}` → `finished` + `download_url`（HMAC 签名、10 分钟有效、单次可用）；`GET .../download?token=&sig=` → 200 JSONL（首行 manifest，其余每行一条留痕），同一链接第二次 → 410。证据：`reports/e2e-evidence.md`「附加」小节与 `reports/pytest-export.log`（12 条用例）。

## 三、实测数字（全部来自脚本输出，不是手写估计）

- `python -m pytest`：**72 passed**，六个测试文件分别为 test_audit_rbac 14 / test_crypto_redaction 13 / test_export 12 / test_hash_chain 13 / test_retention_purge 11 / test_trail_immutable 9；完整日志 `reports/pytest-full.log`，末行 `72 passed in 16.93s`。
- 每个 AC 单独一份日志（路径逐条核对存在于仓库）：`reports/pytest-ac1-immutable.log`、`reports/pytest-ac2-hash-chain.log`、`reports/pytest-ac3-rbac.log`、`reports/pytest-ac4-retention.log`、`reports/pytest-export.log`、`reports/pytest-crypto-redaction.log`；逐条 AC↔用例↔日志对照表 `reports/ac-mapping.md`。
- 发布树验证：取已验证提交 d16040 的完整文件树（codeload tarball；63 个文件 blob sha 与 GitHub tree API 逐条一致）解到独立目录后直接 `python -m pytest` → **72 passed in 30.28s，EXIT=0**，45 个 Python 文件，密钥前缀扫描命中 0。其后的提交只动过 `docs/DELIVERY.md` 与本日志两个文件，其余 61 个文件逐字节未变（`reports/clean-clone.log` 正文写明了这条对应关系，可用 GitHub tree API 复核）。
- 进程级端到端：`python scripts/e2e_evidence.py` 起真实 uvicorn 进程 + 真实 socket/HTTP，跑完 4 条 AC 与导出链路共 26 个请求段落，末行 `EXIT=0`，报告 714 行 / 539 KB（`reports/e2e-evidence.md`，含 uvicorn 访问日志）。
- 代码规模：45 个 Python 文件 / 5485 行（app + tests + migrations + scripts）；README 245 行 / 24279 字节（10 节）；受版本控制文件 63 个（含 `docs/DELIVERY.md` 正文副本）。
- 数据规模（端到端场景）：5 类留痕各 1 条起，AC2 同日 5 条建链、AC4 4 条 200 天前数据被清理。

## 四、提案承诺 → 落地文件（逐条对得上）

| 提案 ② 的文件级清单 | 仓库中的实际路径（已在 git ls-tree 中逐条核对存在） |
| --- | --- |
| 模型 + 迁移 | `app/models/asst_trail.py`（含 AsstTrailCounter / AsstPurgeMarker）、`app/models/asst_hash_chain.py`、`app/models/asst_export_job.py`、`app/models/audit_log.py`、`migrations/versions/0001_initial.py` |
| 5 类留痕写入 | `app/services/trail_writer.py`：record_message / record_answer / record_tool_call / record_blocked_event / record_feedback |
| 加密 + 脱敏 | `app/services/crypto.py`：AES-256-GCM 信封（`enc:v1:`）+ HKDF 派生 + 手机中 4 位/身份证全掩/邮箱留 2 位 + 凭据类键整值 `***` |
| 只增不改 | `app/db/immutability.py`：`BEFORE UPDATE OR DELETE` 触发器，列级白名单仅 `hidden_by_user`（附 typeof 布尔校验）与 `purged_at`；PG16 用 plpgsql `to_jsonb(OLD) - 白名单` 差集，SQLite 用 `BEFORE UPDATE OF <除白名单外全部列>`，两套 DDL 同在，迁移按方言装 |
| 哈希链任务 | `app/workers/hash_chain.py` + `app/services/hash_chain.py`（`row_hash=sha256(prev_hash||canonical_json(payload))`，链按 (org_id, 日期) 分链，链首 64 个 0，链尾+条数写 `audit_log`） |
| 校验 | `app/workers/verify_chain.py`（断点退出码 2，告警可抓）+ `verify_chain()` 返回 first_break_seq/expected/actual |
| 保留期清理 | `app/workers/retention_purge.py` + `app/services/retention.py`（清理窗口/分批/审计摘要） |
| 3 个审计接口 | `app/api/assistant/audit.py`（+ 任务状态与下载两条配套路由） |
| 异步导出 | `app/services/export.py`（JSONL、HMAC 签名令牌、10 分钟、单次可用；每行含 seq/row_hash） |
| 测试 | `tests/` 6 文件 72 条 |
| README / 报告 | `README.md`（10 节）、`reports/`（10 份日志/报告 + 逐条 AC 对照表） |

提案 ⑤ 的字段口径也逐条落库：工具调用含 `tool_name/tool_version/params_redacted/result_digest/result_summary/confirmed_by/confirmed_at/status`；拦截事件含 `block_reason/rule_id`；反馈含 `rating/comment_redacted`；列表接口支持 `user_id/from/to/tool_name/negative_feedback/blocked` 五维筛选（另有 `page/page_size`，上限 200），并对 `user_id+时间` 建复合索引。提案 ⑥ 的成对边界全部有用例：隐藏 vs 物理删除、第 23 条篡改 vs 未篡改、员工 vs 审计角色、200 天 vs 当天、30 天配置 vs 180 天默认、导出链接第 10 分钟 vs 第 11 分钟。

## 五、验证边界（如实说明，未跑通的没有说成跑通）

1. **没有真实 PostgreSQL 16 实例**：本机无 PG、无 Docker，全部自动化测试跑在 SQLite 上。PG 侧触发器用 plpgsql `to_jsonb(OLD) - 白名单` 差集 + `RAISE EXCEPTION`（ERRCODE P0001），Alembic 迁移按方言分支安装，**但这段 DDL 未在真实 PG16 上执行过**，CI 首跑需确认函数与触发器创建成功。README 第 8 节逐项列了 10 条 SQLite/PG 差异（JSON 列 jsonb 键序、UUID 原生类型、并发 seq、触发器实现等）与补救办法。
2. **导出队列未接 Redis**：当前 inline/thread 两种模式已实现并测通，多副本部署需要共享队列（requirements 里已预留 redis 依赖），本单未交付 Redis 队列实现，README 已写明。
3. **导出落盘在本地磁盘**（ASST_EXPORT_DIR），未接对象存储；`export_dir()` 是唯一落盘点，替换成本已降到一处。
4. **S2-AUDIT / S2-IAM 是桩**，按需求文字实现（403 码、字段名、数据范围），未与真实模块联调。
5. **定时任务入口已备但未注册调度**（`python -m app.workers.{hash_chain,verify_chain,retention_purge}`），cron/Airflow 注册属部署侧。
6. **提案 ⑦ 明确不含**：助手对话功能本体、审计工作台前端、SIEM/外部日志平台对接、KMS 集成、历史数据迁移——这些均未做，也未在本正文中声称做了。
7. 一处设计取舍：清理后历史段只保留段尾哈希与条数（`asst_purge_marker`），因此「清理后的链完整性」证明的是「未删除段未被改」，历史段无法逐条复核；若需逐条永久可复核，需把链记录独立归档到审计日志模块，提案 ⑥「需确认②」已标为待确认项。

## 六、交付方式
平台未给交付仓库（github_repo_owner/name 为 null，delivery_workspace_status=none），本单自建公开仓库并以 `github_repo` 资产提交：https://github.com/liaoyuqing/clawhunt-1853-asst-audit-trail 。仓库 public（`private=false`，匿名可 clone），已实跑验证。

提交的 assets 均为可在浏览器直接打开的 https 直链：

| kind | 打开后应看到 |
| --- | --- |
| `github_repo` | 仓库首页，63 个受版本控制文件 |
| `report` | `reports/e2e-evidence.md`：进程级端到端 4 条 AC 与导出链路的逐请求记录（714 行） |
| `report` | `reports/pytest-full.log`：72 passed in 16.93s 的完整 pytest 输出 |
| `doc` | `README.md`：运行方式、环境变量、S2-AUDIT/S2-IAM 对接点、验证边界 |

平台没有通用文件上传口，因此全部交付物既在仓库里逐文件可读，也把关键结论写进了本正文。