# 真实中间件验证报告（PostgreSQL / Redis）

本文件补充 `docs/DELIVERY.md`（首轮已提交正文的逐字快照，**只读**）之外的**真机验证**结果。
首轮交付时的两处缺口——「未在真实 PostgreSQL 上执行过 plpgsql 触发器」与「导出队列只有 inline/thread，
没有跨进程队列」——在本轮补齐，原始输出全部落在 `reports/` 下，可逐行核对。

- 日期：2026-11-27
- 交付工程：本仓库（HEAD 见 `git log`）
- 连接信息：**全部只从环境变量读**。本文档与仓库内任何文件都不含主机、账号、口令（只有 `<host>` / `<password>` 占位符）。

---

## 1. 实测环境

| 项 | 实例实测 | 交付物声明 | 差异说明 |
| --- | --- | --- | --- |
| PostgreSQL | **15.19**（`select version()` 原文见 `reports/pg-migration.log`） | PostgreSQL 16（pgvector） | 需求写 PG16，验证实例是 PG 15.19。本工程用到的能力（`plpgsql` 触发器、`to_jsonb` / `jsonb` 差集、`jsonb_set`、`RETURNING`、`ON CONFLICT`）在 15 与 16 上语义一致；代码里对版本只断言 `>= 15`，不做 16 硬断言，因此 15/16 都能跑。**未在 PG16 实机验证**这一点如实保留。 |
| Redis | **7.2.14**（`INFO server` 原文见 `reports/redis-queue-e2e.log`） | Redis 7 | 一致。实例开启 `requirepass`，连接串必须带密码（只从 `ASST_REDIS_URL` 读）。 |
| 驱动 | psycopg **3.2.3**（不是 psycopg2） | — | DSN scheme 为 `postgresql+psycopg://`；`app/db/session.py` 会把 `postgres://` 归一成 psycopg3 方言。 |
| Python | 3.11.9 | 3.11+ | 一致。 |

---

## 2. PostgreSQL：迁移 + 只增不改触发器

原始输出：`reports/pg-migration.log`（`alembic upgrade head` + `alembic current` + 表/触发器清单 + 触发器实证 + 清理）、
`reports/pg-immutability.log`（触发器实证单独成篇）。

验证方式：连到实例上的既有测试库，**每轮建一个一次性 schema**（示例 `asst_pgverify_<12hex>`），
在空 schema 里跑迁移，测完 `DROP SCHEMA ... CASCADE`，残留计数 0；库里的 `public` 一行未动。

### 2.1 迁移

```
$ python -m alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_initial, 初始结构：...
exit code = 0

$ python -m alembic current
0001_initial (head)
exit code = 0
```

建出 11 张表：`alembic_version, asst_conversation, asst_export_job, asst_export_token, asst_hash_chain,
asst_hash_chain_run, asst_purge_guard, asst_purge_marker, asst_trail, asst_trail_counter, audit_log`；
3 个非内部触发器：`trg_asst_trail_immutable`、`trg_asst_hash_chain_append_only`、`trg_audit_log_append_only`。

### 2.2 本轮修掉的真实缺陷（SQLite 上永远发现不了）

| 位置 | 症状（真机原文） | 原因 | 修法 |
| --- | --- | --- | --- |
| `app/db/immutability.py` `_PG_TRAIL_FN` | `psycopg.errors.SyntaxError: 语法错误 在 "$" 或附近`，`LINE 1: ...RETURNS trigger AS $` | plpgsql 的美元符引用写成了单个 `$` | 改成 `AS $$` / `$$ LANGUAGE plpgsql` |
| `app/db/immutability.py` `_postgres_trail_ddl` / `_postgres_append_only_ddl` | `syntax error at end of input`，`LINE 1: ...trg_asst_trail_immutable BEFORE UPDATE OR DELETE ON asst_trail` | `CREATE TRIGGER ... ON <table>` 与 `FOR EACH ROW EXECUTE FUNCTION ...` 被拆成**两条**语句发送 | 合并成**一条**完整语句 |
| `migrations/env.py` | `ValueError: invalid interpolation syntax in 'postgresql+psycopg://...'` | configparser 把连接串里的 `%` 当插值符（口令含 `#`→`%23`、或 `options=...%20search_path...` 时必现） | 写进 `set_main_option` 前把 `%` 转义成 `%%`（读取侧自动还原，连接串不变） |

这三处在 SQLite 分支都不会被执行，因此首轮 72 条 SQLite 测试全绿也照样漏掉——正是本轮补真机验证的价值。

### 2.3 触发器拦截实证（原文）

```
### 4.1 篡改 payload（UPDATE 非白名单列）—— 应被 plpgsql 触发器 RAISE
UPDATE asst_trail SET payload = '{"content_redacted": "tampered"}'::jsonb WHERE seq = 900001;

psycopg.errors.RaiseException: TRAIL_IMMUTABLE: asst_trail is append-only; only hidden_by_user/purged_at may change

### 4.2 物理删除（purge 窗口未开）—— 应被 RAISE
DELETE FROM asst_trail WHERE seq = 900001;

psycopg.errors.RaiseException: TRAIL_IMMUTABLE: physical delete is reserved for the retention purge window

### 4.3 白名单列 hidden_by_user / purged_at —— 应允许更新
结果：hidden_by_user = True，purged_at 已置 = True —— 白名单两列更新成功

### 4.4 错误布尔值（UPDATE hidden_by_user = 7）
DatatypeMismatch: 字段 "hidden_by_user" 的类型为 boolean, 但表达式的类型为 integer
```

结论：`to_jsonb(OLD) - <白名单列>` 与 `to_jsonb(NEW) - <白名单列>` 做差集 + `RAISE EXCEPTION ... ERRCODE 'P0001'`
在真实 PG 上确实生效；白名单两列放行；非法布尔值由 PG 类型层直接拒绝（SQLite 分支靠触发器的 `typeof` 校验补上）。

---

## 3. Redis：导出队列真的跨进程跑通

原始输出：`reports/redis-queue-e2e.log`（真实 uvicorn + 真实 Redis + 独立 worker 进程）。
测试用例：`tests/test_redis_queue.py`（3 条，`reports/pytest-redis.log`）。

### 3.1 实现

- 新增 `app/services/export_queue.py`：裸 Redis list 队列（不引入 RQ/Celery，生产可平替）。
  - 入队 `LPUSH {prefix}export:queue <job_id>`；
  - 出队 `BRPOP`（worker 侧阻塞取任务）；
  - 在处理集合 `{prefix}export:processing`（worker 取到但未跑完的任务，进程被杀也能看见卡住的）；
  - `process_job` 幂等：只处理 `status=queued` 的任务；失败时 `rollback` 后写 `status=failed` + `error`，不静默丢任务。
- 新增 `app/workers/export_worker.py`：`python -m app.workers.export_worker [--max-jobs N] [--once]`，独立进程、独立 DB 会话。
- `app/services/export.py::enqueue_export` 增加 `redis` 分支：API 进程**只入队**，不执行导出。
- `app/config.py`：`ASST_EXPORT_QUEUE` 允许集合扩到 `{inline, thread, redis}`；`redis` 模式下缺 `ASST_REDIS_URL` 直接 `ConfigError` 拒绝启动。
- 依赖：`requirements.txt` 增加 `redis==8.1.0`（延迟导入，不用 redis 模式时不要求安装）。

### 3.2 端到端实证（节选自 `reports/redis-queue-e2e.log`）

```
## 5. POST /api/assistant/audit/exports（API 只入队）
- HTTP 202（期望 202），status = queued
- worker 未跑时磁盘上的文件：[]（期望 []）
- LLEN asst1853:e2e-xxxxxx:export:queue = 1
- LRANGE queue = ["496b3cc6-...-8064aefdf40c"]

## 6. worker 独立进程消费队列
$ python -m app.workers.export_worker --max-jobs 1 --once
2026-11-27 15:04:18,781 INFO export job 496b3cc6-... finished, rows=5
2026-11-27 15:04:18,781 INFO handled job 496b3cc6-... (status=finished)
2026-11-27 15:04:19,032 INFO export worker exit, handled=1
exit code = 0
- LLEN queue after worker = 0（期望 0）
- processing 列表 after worker = []（期望 []）

## 7. 任务状态与生成文件
- worker 跑完后的文件：["asst-audit-496b3cc6-....jsonl"]
- 文件行数 = 6（1 行 manifest + 5 条留痕）
status = finished / row_count = 5 / file_sha256 = 179d3448...

## 8. 下载导出文件（签名链接单次可用）
- GET download_url -> HTTP 200（期望 200）
- 同一链接再取一次 -> HTTP 410（期望 410）

## 9. 结论
- API 只入队：PASS
- worker 真消费 + 真落文件 + 状态 finished：PASS
- 签名下载单次可用：PASS

## 11. 清理
- 前缀 asst1853:e2e-xxxxxx: 下残留 key = []
- schema asst_e2e_xxxxxxxxxx 已 DROP，残留 = 0
```

上述一轮的数据库是**真实 PostgreSQL**（临时 schema），所以「worker 在另一个进程里改 `asst_export_job.status` 并让 API 读到 `finished`」
跨的是一次真实的网络数据库往返，而不是进程内内存。

### 3.3 队列运行方式

```bash
# API 进程：只入队
export ASST_EXPORT_QUEUE=redis
export ASST_REDIS_URL='redis://:<password>@<host>:<port>/<db>'
export ASST_REDIS_KEY_PREFIX='asst1853:'     # 多业务共用实例时改成各自的隔离前缀
export ASST_DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:<port>/<db>'
export ASST_TRAIL_KEY='<trail key>'
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# worker 进程（可与 API 分机部署，同一套环境变量）
python -m app.workers.export_worker                 # 常驻
python -m app.workers.export_worker --max-jobs 1 --once   # 一次性消费 1 个后退出（验证/定时任务用）
```

---

## 4. 测试矩阵（本轮实测）

| 配置 | 命令 | 结果 | 日志 |
| --- | --- | --- | --- |
| 离线基线（临时 SQLite + inline） | `python -m pytest -q` | **72 passed, 13 skipped** | `reports/pytest-sqlite.log` |
| 接真实 PostgreSQL | `ASST_TEST_DATABASE_URL=... python -m pytest -q` | **82 passed, 3 skipped**（85 collected；3 条 Redis 用例因未给 `ASST_TEST_REDIS_URL` 而 skip） | `reports/pytest-pg.log` |
| 接真实 Redis | `ASST_TEST_REDIS_URL=... python -m pytest -q tests/test_redis_queue.py` | **3 passed** | `reports/pytest-redis.log` |

- PG 模式：每个用例建独立 schema（`asst_test_<12hex>`）并 `search_path` 指过去，用例结束 `DROP SCHEMA CASCADE`。
  这样并行跑、与库里既有数据共存都不打架；跑完残留 schema 计数 0。
- Redis 模式：只用 `asst1853:` 前缀下的两个 key，用例结束 `DEL`（`reports/pytest-redis.log` 的清理段可见残留为空）。
- 新增用例：`tests/test_pg_dialect.py`（10 条 PG 方言/触发器/JSONB/UUID 往返/篡改检测）、
  `tests/test_redis_queue.py`（3 条：入队-出队-落盘-状态回写、重复入队幂等、非 queued 跳过）。
  这两组在没给中间件环境变量时自动 skip，**不改变离线基线的 72 条**。

---

## 5. 与首轮交付正文（`docs/DELIVERY.md` 第 5 节）的对照

`docs/DELIVERY.md` 是首轮**已提交**的正文逐字快照，按 A.3 第 9 条只读、一字未改。
它第 5 节列的 7 条验证边界，本轮变化如下：

| 首轮声明的边界 | 本轮状态 |
| --- | --- |
| 1. 无真实 PG16 实例，测试全跑 SQLite，PG 分支已写未执行 | **已补**：真实 PostgreSQL **15.19** 上 `alembic upgrade head` + 触发器实证 + 82 passed（3 条 Redis 用例 skip，另跑为 3 passed）。仍未在 16 上跑（实例只有 15.19），差异见本文第 1 节。 |
| 2. 导出队列未接 Redis | **已补**：裸 Redis list 队列 + 独立 worker 进程，端到端 PASS（本文第 3 节）。 |
| 3. 导出落本地盘，未接对象存储 | 未变（`app/services/export.py::export_dir()` 仍是唯一落盘点）。 |
| 4. S2-AUDIT / S2-IAM 为桩 | 未变（对接点见 README 第 5 节）。 |
| 5. 定时任务未注册 cron | 未变（入口 `python -m app.workers.*` 已备）。 |
| 6. 清理后历史段只留段尾哈希与条数 | 未变（`asst_purge_marker` 设计如此）。 |
| 7.（正文第五节其余条目） | 未变。 |

---

## 6. 结论

- 只增不改的 DB 层护栏在**真实 PostgreSQL** 上确实建得出、也确实拦得住（UPDATE 非白名单列、DELETE 都 RAISE）；
  白名单两列可改；非法布尔值被类型层拒绝。
- 导出队列在**真实 Redis** 上真的跨进程工作：API 只入队 → 独立 worker 出队执行 → 文件落盘 → 状态回写 → 签名链接可下载（单次）。
- 离线基线不降级：仍是 72 passed（+13 skip 的中间件专项用例）；接真 PG 时 85 collected / 82 passed + 3 skipped，Redis 三条单独跑 3 passed。
