# 端到端取证（真实 uvicorn 进程 + 真实 HTTP）

- 生成时间：2026-11-27T10:31:10+0800
- 启动命令：`python -m uvicorn app.main:app --host 127.0.0.1 --port 56718`
- 建表方式：`python -m alembic upgrade head`
- 数据库：SQLite 临时文件（本机无 PostgreSQL 16 实例，差异见 README 第 8 节）
- 身份来源：S2-IAM 桩（`ASST_IAM_STUB_TOKENS` 映射到虚构 UUID）


## 环境自检

### GET /healthz

- 请求：`GET /healthz`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "status": "ok"
}
```


## AC1 用户删除会话后，审计接口仍能查到该会话完整内容

### 1-1 新建会话

- 请求：`POST /api/assistant/conversations`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
  "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
  "title": "E2E 虚构会话"
}
```

### 1-2 写用户消息（原文加密 + 脱敏文本）

- 请求：`POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "2bccdfcb-b569-4863-94d3-75a66faadff3",
  "seq": 1,
  "kind": "message",
  "content_sha256": "16960afbd29000fb23a21702c5c3688aec5efffa20cf7423579ec8b738f55fe9"
}
```

### 1-3 写回答（引用 doc_id 与版本、模型名、提示词版本）

- 请求：`POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/answers`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "cebaa4e2-23d9-4aca-a45d-977dc4c61dfc",
  "seq": 2,
  "kind": "answer",
  "content_sha256": "45c5074aa2ce69fbe5a722e4e1fc5ba0fd86e3a93d90a75a04af2d9471adbdcd"
}
```

### 1-4 写工具调用（脱敏参数 + 结果摘要 + 确认人与时间）

- 请求：`POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/tool-calls`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "3893a671-2457-4f40-af64-7d21703cee39",
  "seq": 3,
  "kind": "tool_call",
  "content_sha256": null
}
```

### 1-5 写关停开关拦截事件

- 请求：`POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/blocked-events`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "fa823a29-3ea6-4924-b754-47a49873b239",
  "seq": 4,
  "kind": "blocked_event",
  "content_sha256": null
}
```

### 1-6 写用户反馈（差评）

- 请求：`POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/feedback`
- 状态码：**201**（期望 201，OK）
- 响应：

```json
{
  "id": "3b8c28a5-9c7c-4faf-953f-249405169a1e",
  "seq": 5,
  "kind": "feedback",
  "content_sha256": null
}
```

### 1-7 删除前：审计详情

- 请求：`GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
  "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
  "title": "E2E 虚构会话",
  "hidden_by_user": false,
  "deleted_at": null,
  "created_at": "2026-11-27T02:31:12.719055",
  "trail_count": 5,
  "trails": [
    {
      "id": "2bccdfcb-b569-4863-94d3-75a66faadff3",
      "seq": 1,
      "kind": "message",
      "occurred_at": "2026-11-27T02:31:12.734957",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": false,
      "content": "帮我查会员 13812345678 的对接进度",
      "content_redacted": "帮我查会员 138****5678 的对接进度",
      "content_sha256": "16960afbd29000fb23a21702c5c3688aec5efffa20cf7423579ec8b738f55fe9",
      "doc_refs": null,
      "model_name": null,
      "prompt_version": null,
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "cebaa4e2-23d9-4aca-a45d-977dc4c61dfc",
      "seq": 2,
      "kind": "answer",
      "occurred_at": "2026-11-27T02:31:12.755523",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": false,
      "content": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_redacted": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_sha256": "45c5074aa2ce69fbe5a722e4e1fc5ba0fd86e3a93d90a75a04af2d9471adbdcd",
      "doc_refs": [
        {
          "doc_id": "doc-0001",
          "doc_version": "v3",
          "title": "虚构制度"
        }
      ],
      "model_name": "stub-model-1",
      "prompt_version": "p-2026-01",
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "3893a671-2457-4f40-af64-7d21703cee39",
      "seq": 3,
      "kind": "tool_call",
      "occurred_at": "2026-11-27T02:31:12.772051",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": false,
      "content": null,
      "content_redacted": null,
      "content_sha256": null,
      "doc_refs": null,
      "model_name": null,
      "prompt_version": nul
... <truncated>
```

### 1-8 界面「删除会话」（只置 hidden_by_user，204）

- 请求：`DELETE /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**204**（期望 204，OK）
- 响应：

```json
""
```

### 1-9 删除后：审计详情仍 200，条数不变，内容解密后完整

- 请求：`GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
  "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
  "title": "E2E 虚构会话",
  "hidden_by_user": true,
  "deleted_at": "2026-11-27T02:31:12.837194",
  "created_at": "2026-11-27T02:31:12.719055",
  "trail_count": 5,
  "trails": [
    {
      "id": "2bccdfcb-b569-4863-94d3-75a66faadff3",
      "seq": 1,
      "kind": "message",
      "occurred_at": "2026-11-27T02:31:12.734957",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": "帮我查会员 13812345678 的对接进度",
      "content_redacted": "帮我查会员 138****5678 的对接进度",
      "content_sha256": "16960afbd29000fb23a21702c5c3688aec5efffa20cf7423579ec8b738f55fe9",
      "doc_refs": null,
      "model_name": null,
      "prompt_version": null,
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "cebaa4e2-23d9-4aca-a45d-977dc4c61dfc",
      "seq": 2,
      "kind": "answer",
      "occurred_at": "2026-11-27T02:31:12.755523",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_redacted": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_sha256": "45c5074aa2ce69fbe5a722e4e1fc5ba0fd86e3a93d90a75a04af2d9471adbdcd",
      "doc_refs": [
        {
          "doc_id": "doc-0001",
          "doc_version": "v3",
          "title": "虚构制度"
        }
      ],
      "model_name": "stub-model-1",
      "prompt_version": "p-2026-01",
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "3893a671-2457-4f40-af64-7d21703cee39",
      "seq": 3,
      "kind": "tool_call",
      "occurred_at": "2026-11-27T02:31:12.772051",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": null,
      "content_redacted": null,
      "content_sha256": null,
      "doc_refs": null,
      "model_name": null,
      "
... <truncated>
```


**核对**：`hidden_by_user=True`，删除前后 `trail_count` 均为 5（一致=True），解密原文逐字一致=True，脱敏文本掩码生效=True


## AC2 篡改一条历史留痕后，哈希链校验任务报告断点位置

### 2-1 计算指定日期的哈希链

- 请求：`POST /api/assistant/ops/hash-chain/build`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "org_id": "56ee39d4-c039-4abf-b6a7-a3e5a2459460",
  "chain_date": "2026-11-24",
  "rows": 5,
  "ok": true,
  "status": "ok",
  "first_break_seq": null,
  "break_reason": null,
  "expected_hash": null,
  "actual_hash": null,
  "chain_head": "105e9a8455af057a683d725bb2fc5085772edf9b63e825a496847fa5e650af42",
  "chain_tail_hash": "e11fc2f84c37c63f88fa803c396e4a33c4f3a6db4a3644b3ee9031fe9fe246e1",
  "checked_rows": 5,
  "skipped_purged_rows": 0,
  "message": ""
}
```

### 2-2 未篡改时校验（应 ok）

- 请求：`POST /api/assistant/ops/hash-chain/verify`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "org_id": "56ee39d4-c039-4abf-b6a7-a3e5a2459460",
  "chain_date": "2026-11-24",
  "rows": 5,
  "ok": true,
  "status": "ok",
  "first_break_seq": null,
  "break_reason": null,
  "expected_hash": null,
  "actual_hash": null,
  "chain_head": "105e9a8455af057a683d725bb2fc5085772edf9b63e825a496847fa5e650af42",
  "chain_tail_hash": "e11fc2f84c37c63f88fa803c396e4a33c4f3a6db4a3644b3ee9031fe9fe246e1",
  "checked_rows": 5,
  "skipped_purged_rows": 0,
  "message": "链完整：5 条已复核，跳过 0 条已清理行"
}
```


**篡改步骤**：先 `DROP TRIGGER trg_asst_trail_no_update`（模拟超级用户绕过），再 `UPDATE asst_trail SET payload = json_set(payload, '$.content_redacted', 'TAMPERED-BY-E2E') WHERE seq = 8`

### 2-3 篡改后校验（应报断点）

- 请求：`POST /api/assistant/ops/hash-chain/verify`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "org_id": "56ee39d4-c039-4abf-b6a7-a3e5a2459460",
  "chain_date": "2026-11-24",
  "rows": 5,
  "ok": false,
  "status": "broken",
  "first_break_seq": 3,
  "break_reason": "payload_changed",
  "expected_hash": "965922b10719eeabc1370a68c5c0517bff943b3b23a9666b27215f1583edb4e4",
  "actual_hash": "1f5ac9ede1d4ce72ecf4567c6aa4d4448fd13a4b7d29aa4115ef8e7375ac7a1e",
  "chain_head": "105e9a8455af057a683d725bb2fc5085772edf9b63e825a496847fa5e650af42",
  "chain_tail_hash": "e11fc2f84c37c63f88fa803c396e4a33c4f3a6db4a3644b3ee9031fe9fe246e1",
  "checked_rows": 2,
  "skipped_purged_rows": 0,
  "message": "链在第 3 条断裂（payload_changed）"
}
```


**核对**：`ok=False`，`first_break_seq=3`，`break_reason=payload_changed`，expected≠actual = True


## AC3 非审计角色 403；审计角色每次查看产生一条审计记录

### 3-1 员工角色查审计列表（期望 403）

- 请求：`GET /api/assistant/audit/conversations`
- 状态码：**403**（期望 403，OK）
- 响应：

```json
{
  "detail": {
    "code": "FORBIDDEN_AUDIT_ROLE",
    "message": "audit endpoints require the auditor role; current roles: employee",
    "role": [
      "employee"
    ]
  }
}
```

### 3-2 员工角色查审计详情（期望 403）

- 请求：`GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**403**（期望 403，OK）
- 响应：

```json
{
  "detail": {
    "code": "FORBIDDEN_AUDIT_ROLE",
    "message": "audit endpoints require the auditor role; current roles: employee",
    "role": [
      "employee"
    ]
  }
}
```

### 3-3 员工角色建导出（期望 403）

- 请求：`POST /api/assistant/audit/exports`
- 状态码：**403**（期望 403，OK）
- 响应：

```json
{
  "detail": {
    "code": "FORBIDDEN_AUDIT_ROLE",
    "message": "audit endpoints require the auditor role; current roles: employee",
    "role": [
      "employee"
    ]
  }
}
```

### 3-4 无 Authorization 头（期望 401）

- 请求：`GET /api/assistant/audit/conversations`
- 状态码：**401**（期望 401，OK）
- 响应：

```json
{
  "detail": {
    "code": "UNAUTHENTICATED",
    "message": "缺少 Authorization: Bearer <token>"
  }
}
```

### 3-5 只给 X-Api-Key（期望 401，只认 Bearer）

- 请求：`GET /api/assistant/audit/conversations`
- 状态码：**401**（期望 401，OK）
- 响应：

```json
{
  "detail": {
    "code": "UNAUTHENTICATED",
    "message": "缺少 Authorization: Bearer <token>"
  }
}
```

### 3-6 审计角色第 1 次查列表（期望 200）

- 请求：`GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "items": [
    {
      "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "title": "E2E 虚构会话",
      "hidden_by_user": true,
      "deleted_at": "2026-11-27T02:31:12.837194",
      "created_at": "2026-11-27T02:31:12.719055",
      "trail_count": 10,
      "first_occurred_at": "2026-11-24T10:00:00",
      "last_occurred_at": "2026-11-27T02:31:12.805627",
      "tool_names": [
        "crm.member.lookup"
      ],
      "negative_feedback_count": 1,
      "blocked_event_count": 1
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10,
  "pages": 1
}
```

### 3-6 审计角色第 2 次查列表（期望 200）

- 请求：`GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "items": [
    {
      "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "title": "E2E 虚构会话",
      "hidden_by_user": true,
      "deleted_at": "2026-11-27T02:31:12.837194",
      "created_at": "2026-11-27T02:31:12.719055",
      "trail_count": 10,
      "first_occurred_at": "2026-11-24T10:00:00",
      "last_occurred_at": "2026-11-27T02:31:12.805627",
      "tool_names": [
        "crm.member.lookup"
      ],
      "negative_feedback_count": 1,
      "blocked_event_count": 1
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10,
  "pages": 1
}
```

### 3-6 审计角色第 3 次查列表（期望 200）

- 请求：`GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "items": [
    {
      "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "title": "E2E 虚构会话",
      "hidden_by_user": true,
      "deleted_at": "2026-11-27T02:31:12.837194",
      "created_at": "2026-11-27T02:31:12.719055",
      "trail_count": 10,
      "first_occurred_at": "2026-11-24T10:00:00",
      "last_occurred_at": "2026-11-27T02:31:12.805627",
      "tool_names": [
        "crm.member.lookup"
      ],
      "negative_feedback_count": 1,
      "blocked_event_count": 1
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10,
  "pages": 1
}
```


**核对**：查看前 `audit_view` = 2，连查 3 次后 = 5，增量 = 3（期望 3）

### 3-7 跨 org 的审计角色查本 org 会话（期望 404，防枚举）

- 请求：`GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**404**（期望 404，OK）
- 响应：

```json
{
  "detail": {
    "code": "CONVERSATION_NOT_FOUND",
    "message": "会话不存在或不在数据范围内"
  }
}
```


## AC4 到期数据清理后，审计日志中有删除摘要

### 4-1 触发保留期清理（默认 180 天）

- 请求：`POST /api/assistant/ops/retention/purge`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "org_id": "56ee39d4-c039-4abf-b6a7-a3e5a2459460",
  "retention_days": 180,
  "cutoff": "2026-05-31T02:31:13.120950",
  "deleted_rows": 4,
  "deleted_chain_entries": 0,
  "batches": 1,
  "range_from": "2026-05-11T09:00:00",
  "range_to": "2026-05-11T09:03:00",
  "chain_tail_hash": "44df3a9afbdd1a7b4dbf50c67c143fdc4c1b8ac55e026898f74aef0c81830883",
  "audit_log_id": "37e543ec-524b-4421-8480-adae2e7884ae"
}
```

### 4-2 清理后再查审计详情（近期留痕仍在）

- 请求：`GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
  "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
  "title": "E2E 虚构会话",
  "hidden_by_user": true,
  "deleted_at": "2026-11-27T02:31:12.837194",
  "created_at": "2026-11-27T02:31:12.719055",
  "trail_count": 10,
  "trails": [
    {
      "id": "2bccdfcb-b569-4863-94d3-75a66faadff3",
      "seq": 1,
      "kind": "message",
      "occurred_at": "2026-11-27T02:31:12.734957",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": "帮我查会员 13812345678 的对接进度",
      "content_redacted": "帮我查会员 138****5678 的对接进度",
      "content_sha256": "16960afbd29000fb23a21702c5c3688aec5efffa20cf7423579ec8b738f55fe9",
      "doc_refs": null,
      "model_name": null,
      "prompt_version": null,
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "cebaa4e2-23d9-4aca-a45d-977dc4c61dfc",
      "seq": 2,
      "kind": "answer",
      "occurred_at": "2026-11-27T02:31:12.755523",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_redacted": "按虚构制度 v3 第 4 条，该会员可对接。",
      "content_sha256": "45c5074aa2ce69fbe5a722e4e1fc5ba0fd86e3a93d90a75a04af2d9471adbdcd",
      "doc_refs": [
        {
          "doc_id": "doc-0001",
          "doc_version": "v3",
          "title": "虚构制度"
        }
      ],
      "model_name": "stub-model-1",
      "prompt_version": "p-2026-01",
      "tool_name": null,
      "tool_version": null,
      "params_redacted": null,
      "result_summary": null,
      "confirmed_by": null,
      "confirmed_at": null,
      "status": null,
      "block_reason": null,
      "rule_id": null,
      "rating": null,
      "comment_redacted": null
    },
    {
      "id": "3893a671-2457-4f40-af64-7d21703cee39",
      "seq": 3,
      "kind": "tool_call",
      "occurred_at": "2026-11-27T02:31:12.772051",
      "user_id": "233ac31b-398d-438b-9af8-6fdb6b5d605e",
      "conversation_id": "26cf1d7b-4c8d-4b17-884e-d8d49616b29e",
      "hidden_by_user": true,
      "content": null,
      "content_redacted": null,
      "content_sha256": null,
      "doc_refs": null,
      "model_name": null,
      
... <truncated>
```


**核对**：清理摘要 = {"org_id": "56ee39d4-c039-4abf-b6a7-a3e5a2459460", "retention_days": 180, "cutoff": "2026-05-31T02:31:13.120950", "deleted_rows": 4, "deleted_chain_entries": 0, "batches": 1, "range_from": "2026-05-11T09:00:00", "range_to": "2026-05-11T09:03:00", "chain_tail_hash": "44df3a9afbdd1a7b4dbf50c67c143fdc4c1b8ac55e026898f74aef0c81830883", "audit_log_id": "37e543ec-524b-4421-8480-adae2e7884ae"}

`audit_log` 里的删除摘要：

```json
[
  {
    "count": 4,
    "retention_days": 180,
    "cutoff": "2026-05-31T02:31:13.120950",
    "range_from": "2026-05-11T09:00:00",
    "range_to": "2026-05-11T09:03:00",
    "chain_tail_hash": "44df3a9afbdd1a7b4dbf50c67c143fdc4c1b8ac55e026898f74aef0c81830883",
    "chain_entries_deleted": 0,
    "batches": 1
  }
]
```


**核对结论**：删除摘要四要素（count / range_from / range_to / retention_days）齐全=True


## 附加：异步 JSONL 导出 + 10 分钟签名下载链接

### 5-1 POST /api/assistant/audit/exports（202 + job_id）

- 请求：`POST /api/assistant/audit/exports`
- 状态码：**202**（期望 202，OK）
- 响应：

```json
{
  "job_id": "50da9f09-69fa-42f9-b7bf-a6442b28a6e8",
  "status": "finished",
  "message": "导出任务已受理；用 GET /api/assistant/audit/exports/{job_id} 取下载链接（10 分钟有效）"
}
```

### 5-2 GET 任务状态（finished + download_url）

- 请求：`GET /api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8`
- 状态码：**200**（期望 200，OK）
- 响应：

```json
{
  "job_id": "50da9f09-69fa-42f9-b7bf-a6442b28a6e8",
  "status": "finished",
  "format": "jsonl",
  "row_count": 10,
  "file_sha256": "654b1539d6dc4bdd361c386dfbd3ba7515e3b1fae79c3b0bdea363f5702110b3",
  "download_url": "http://127.0.0.1:56718/api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8/download?token=50da9f09-69fa-42f9-b7bf-a6442b28a6e8.6f687c16308b4f21a6d0a89d8eb7b2f3&sig=aa8283d8847c4a86d162d8e81f87c37dc6c98bed99338c719186f07ee89b8265",
  "expires_at": "2026-11-27T02:41:13.182584",
  "error": null
}
```


**下载**：`GET /api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8/download?token=50da9f09-69fa-42f9-b7bf-a6442b28a6e8.6f687c16308b4f21a6d0a89d8eb7b2f3&sig=<redacted>` → **200**，共 11 行（1 行 manifest + 10 条留痕），首行类型 = `manifest`

**单次可用**：同一链接第二次下载 → **410**（期望 410）


## 结论

- 失败项计数：**0**
- 判定：全部符合预期


## 服务端访问日志（uvicorn）

```
INFO:     Started server process [28652]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:56718 (Press CTRL+C to quit)
INFO:     127.0.0.1:56720 - "GET /healthz HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /healthz HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/answers HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/tool-calls HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/blocked-events HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/feedback HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "DELETE /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 204 No Content
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/ops/hash-chain/build HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "POST /api/assistant/ops/hash-chain/verify HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "POST /api/assistant/ops/hash-chain/verify HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations HTTP/1.1" 403 Forbidden
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 403 Forbidden
INFO:     127.0.0.1:56725 - "POST /api/assistant/audit/exports HTTP/1.1" 403 Forbidden
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations?page_size=10&tool_name=crm.member.lookup HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e/messages HTTP/1.1" 201 Created
INFO:     127.0.0.1:56725 - "POST /api/assistant/ops/retention/purge HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/conversations/26cf1d7b-4c8d-4b17-884e-d8d49616b29e HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "POST /api/assistant/audit/exports HTTP/1.1" 202 Accepted
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8 HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8/download?token=50da9f09-69fa-42f9-b7bf-a6442b28a6e8.6f687c16308b4f21a6d0a89d8eb7b2f3&sig=aa8283d8847c4a86d162d8e81f87c37dc6c98bed99338c719186f07ee89b8265 HTTP/1.1" 200 OK
INFO:     127.0.0.1:56725 - "GET /api/assistant/audit/exports/50da9f09-69fa-42f9-b7bf-a6442b28a6e8/download?token=50da9f09-69fa-42f9-b7bf-a6442b28a6e8.6f687c16308b4f21a6d0a89d8eb7b2f3&sig=aa8283d8847c4a86d162d8e81f87c37dc6c98bed99338c719186f07ee89b8265 HTTP/1.1" 410 Gone
```

