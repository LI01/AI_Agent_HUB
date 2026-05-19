# Design: Agent Hub Chat — 嘉骏科技 AI 培训实操平台

**Date:** 2026-05-19
**Status:** Draft
**Author:** Brainstorming Session

## 1. Overview

Build a web-based training platform for 嘉骏科技's 2-day AI training program (30-50 trainees, 12-15 groups). Trainees log in via Cloudflare Access and get a hands-on environment where they can chat with AI agents, complete role-based training tasks, upload files/directories, and produce deliverables — all within the Agent Hub ecosystem they're learning about.

**Training context:** The platform supports 4 role groups from the training curriculum:
- **PM 组** (Groups 1-4): Write project proposals using AI
- **开发组** (Groups 5-8): Write code + Code Review using AI
- **硬件组** (Groups 9-11): Analyze fault logs + write debugging reports
- **销售/管理组** (Groups 12-15): Customer proposals + 8D quality reports

**Approach:** Scheme A — lightweight extension of the existing Hub, reusing the task system, WebSocket infrastructure, and Cloudflare Access JWT authentication.

## 2. Authentication & User Model

### 2.1 Auth Flow

1. User visits `/chat` — Cloudflare Access intercepts, requires login.
2. After login, Access forwards `Cf-Access-Jwt-Assertion` header to Hub.
3. Hub validates JWT (reuses existing `verify_cf_access_jwt`), checks email domain against `AGENT_HUB_CF_ACCESS_ALLOWED_DOMAINS` (`leopardimaging.com`, `aglaiasense.com`).
4. Hub extracts `email` and `name` from JWT claims, upserts into `users` table.
5. Admin status: email checked against `AGENT_HUB_ADMIN_EMAILS` env var.

### 2.2 New Database Tables

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,              -- ULID
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    role TEXT,                        -- 'pm' / 'developer' / 'hardware' / 'sales' / 'admin'
    group_number INTEGER,             -- 1-15, assigned by instructor via admin panel
    is_admin INTEGER DEFAULT 0,
    is_instructor INTEGER DEFAULT 0,  --讲师/管理员
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,              -- ULID
    user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT,                       -- Auto-generated: first 50 chars of first message
    agent_id TEXT NOT NULL,
    task_template_id TEXT,            -- Links to training task template
    status TEXT DEFAULT 'active',     -- active / submitted / archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,              -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,               -- 'user' / 'agent' / 'system'
    content TEXT,
    files JSON,                       -- [{path, content_base64, size}]
    task_id TEXT,                     -- Linked task id (for agent messages)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Training task templates (pre-loaded from training curriculum)
CREATE TABLE task_templates (
    id TEXT PRIMARY KEY,              -- ULID
    role TEXT NOT NULL,               -- 'pm' / 'developer' / 'hardware' / 'sales'
    title TEXT NOT NULL,
    description TEXT,                 -- Full task description with steps
    expected_output TEXT,             -- What the trainee should produce
    skill_template TEXT,              -- The skill template markdown content
    time_limit_minutes INTEGER,       -- Suggested time limit
    sort_order INTEGER                -- Display order
);

-- Training submissions (for instructor review)
CREATE TABLE submissions (
    id TEXT PRIMARY KEY,              -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score INTEGER,                    -- Instructor score (0-100)
    feedback TEXT,                    -- Instructor feedback
    status TEXT DEFAULT 'submitted'   -- submitted / reviewed
);
```

### 2.3 Indexes

- `conversations(user_id, updated_at DESC)`
- `messages(conversation_id, created_at ASC)`
- `messages(task_id)`
- `task_templates(role, sort_order)`
- `submissions(user_id, status)`

## 3. Training Task Templates

Pre-loaded from the training curriculum. Each role gets specific task templates:

### 3.1 PM 组 Templates

| Template | Title | Time | Expected Output |
|----------|-------|------|-----------------|
| pm-proposal | 项目提案：智能工厂改造 | 75min | 项目提案 Word 文档 |
| pm-skill-design | PM Skill 设计草稿 | 15min | Skill 设计文档 |

### 3.2 开发组 Templates

| Template | Title | Time | Expected Output |
|----------|-------|------|-----------------|
| dev-code | 文件批量处理工具 | 60min | Python 脚本 + 单元测试 |
| dev-review | Code Review 报告 | 15min | Code Review 文档 |

### 3.3 硬件组 Templates

| Template | Title | Time | Expected Output |
|----------|-------|------|-----------------|
| hw-fault-analysis | GS500 故障日志分析 | 50min | 故障分析报告 + 调试报告 |

### 3.4 销售/管理组 Templates

| Template | Title | Time | Expected Output |
|----------|-------|------|-----------------|
| sales-proposal | 客户方案 + PPT | 30min | 客户方案文档 |
| quality-8d | 8D 品质报告 | 30min | 8D 报告文档 |

### 3.5 Template Loading

Templates are loaded into the database on first run via a seed script:
```bash
python -m hub.seed_training_templates
```

## 4. User Workspace & File Storage

### 4.1 Directory Structure

Each user gets a private workspace:
```
/data/user-workspaces/{user_id}/
├── pm-proposal/          # Per-template subdirectories
├── dev-code/
├── hw-fault-analysis/
└── ...
```

All file operations are scoped to this directory. Path traversal (`..`, absolute paths) is rejected.

### 4.2 Training Starter Files

Each task template can include starter files that are copied to the user's workspace when they start the task:
- **PM 组**: 需求文档模板、历史项目案例
- **开发组**: 示例代码框架、测试模板
- **硬件组**: 故障日志样例、设备参数文档
- **销售/管理组**: 客户需求模板、8D 报告模板

Starter files are stored at `/data/training-templates/{template_id}/` and copied on task start.

### 4.3 File Upload

- **Small files** (≤ 10 MiB single, ≤ 50 MiB directory): sent inline via WebSocket as base64 in `chat_message.files`.
- **Large files**: `POST /chat/upload` with multipart chunked upload (5 MiB chunks).
- Files stored at `/data/user-workspaces/{user_id}/{path}`.
- Configurable limits: `AGENT_HUB_CHAT_MAX_FILE_SIZE` (default 10 MiB), `AGENT_HUB_CHAT_MAX_DIR_SIZE` (default 50 MiB).

### 4.4 File API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/chat/files?path=src/` | List directory contents |
| GET | `/chat/files?path=src/main.py` | Download file |
| POST | `/chat/upload` | Upload file/directory (chunked) |
| DELETE | `/chat/files?path=old.py` | Delete file |
| POST | `/chat/files/copy-template` | Copy template starter files to workspace |

### 4.5 Agent Access

When a chat task is dispatched to an agent, the task payload includes:
```json
{
  "workdir": "/data/user-workspaces/{user_id}/",
  "files": [...]
}
```
The agent can read/write files in this directory. Results are read back into the conversation.

## 5. WebSocket Protocol

### 5.1 Endpoint

`WS /chat/ws?token=<cf-jwt>`

### 5.2 Client → Hub Messages

```json
// Send a chat message
{
  "type": "chat_message",
  "conversation_id": "conv-abc123",
  "content": "帮我看看这段代码",
  "files": [{"path": "src/main.py", "content_base64": "...", "size": 1024}]
}
```

### 5.3 Hub → Client Messages

```json
// Acknowledge receipt
{"type": "message_ack", "message_id": "msg-001"}

// Agent status update
{"type": "agent_status", "status": "thinking"}   // or "done", "error"

// Agent reply
{
  "type": "agent_message",
  "message_id": "msg-002",
  "content": "这段代码的问题是...",
  "files": []
}
```

### 5.4 Internal Flow

1. Hub receives `chat_message` → persists to `messages` (role=user).
2. Creates a task via existing `POST /tasks` flow, payload includes user message + files + recent conversation context (last 5 messages) + skill template context.
3. Task dispatched to selected agent via existing agent WebSocket.
4. Agent reports result → Hub persists to `messages` (role=agent) → pushes to client via chat WebSocket.

## 6. REST API

### 6.1 New Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/chat` | CF JWT | Chat landing page (HTML) |
| GET | `/chat/{conv_id}` | CF JWT | Single conversation page (HTML) |
| GET | `/chat/conversations` | CF JWT | List user's conversations |
| POST | `/chat/conversations` | CF JWT | Create new conversation (from template or blank) |
| GET | `/chat/conversations/{id}/messages` | CF JWT | Get conversation history |
| POST | `/chat/upload` | CF JWT | File/directory upload |
| GET | `/chat/files` | CF JWT | Browse user workspace |
| GET | `/chat/files/*` | CF JWT | Download file |
| DELETE | `/chat/files/*` | CF JWT | Delete file |
| POST | `/chat/files/copy-template` | CF JWT | Copy template starter files |
| GET | `/chat/agents/available` | CF JWT | List available agents |
| GET | `/chat/templates` | CF JWT | List task templates (filtered by user role) |
| GET | `/chat/templates/{id}` | CF JWT | Get template details |
| POST | `/chat/conversations/{id}/submit` | CF JWT | Submit work for instructor review |

### 6.2 Admin/Instructor Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/admin/chat/users` | Admin JWT | List all users |
| POST | `/admin/chat/users/{id}/role` | Admin JWT | Set user role/group |
| POST | `/admin/chat/users/{id}/disable` | Admin JWT | Disable user |
| POST | `/admin/chat/users/{id}/enable` | Admin JWT | Enable user |
| GET | `/admin/chat/users/{id}/conversations` | Admin JWT | View user's conversations (read-only) |
| GET | `/admin/chat/agents` | Admin JWT | List all agents with status |
| POST | `/admin/chat/agents` | Admin JWT | Create new agent |
| POST | `/admin/chat/agents/{id}/restart` | Admin JWT | Restart agent |
| POST | `/admin/chat/agents/{id}/offline` | Admin JWT | Force agent offline |
| DELETE | `/admin/chat/agents/{id}` | Admin JWT | Delete agent |
| GET | `/admin/chat/submissions` | Admin JWT | List all submissions |
| GET | `/admin/chat/submissions/{id}` | Admin JWT | View submission details |
| POST | `/admin/chat/submissions/{id}/score` | Admin JWT | Score a submission |
| GET | `/admin/chat/dashboard` | Admin JWT | Training dashboard (progress, scores, activity) |

### 6.3 Agent Creation (`POST /admin/chat/agents`)

Request:
```json
{
  "name": "my-new-agent",
  "role": "coder",
  "cli": "opencode",
  "capabilities": ["python", "javascript"],
  "max_concurrent_tasks": 1,
  "timeout_minutes": 30
}
```

Response:
```json
{
  "agent_id": "opencode-coder-hostname",
  "api_key": "sk-raw-key-returned-once",
  "install_command": "AGENT_HUB_API_KEY=sk-raw-key... AGENT_HUB_URL=... bash scripts/install_agent_services.sh"
}
```

## 7. Frontend Design

### 7.1 Trainee View

```
┌──────────────────────────────────────────────────────────────┐
│ 嘉骏 AI 培训平台                              [👤 张三]      │
├──────────────┬───────────────────────────────────────────────┤
│ 📋 我的任务   │  📝 任务：项目提案 — 智能工厂改造              │
│              │  ⏱ 建议时间：75 分钟                           │
│ ● 项目提案    │                                              │
│   进行中      │  ┌────────────────────────────────────────┐  │
│ ○ Code Review │  │ 🤖 Agent: 你好！我来帮你写项目提案。   │  │
│               │  │ 请先告诉我项目的具体需求和背景。       │  │
│ [+ 新任务]    │  └────────────────────────────────────────┘  │
│              │  ┌────────────────────────────────────────┐  │
│ 📂 工作区     │  │ 👤 张三: 这是一个智能工厂改造项目...   │  │
│ /pm-proposal/ │  │ 📎 requirements.docx                   │  │
│  📎 req.docx  │  └────────────────────────────────────────┘  │
│  📎 case.pdf  │  ┌────────────────────────────────────────┐  │
│              │  │ 🤖 Agent: 根据需求，我生成了提案大纲... │  │
│ [提交作业]    │  │ 📎 proposal-draft.md                     │  │
│              │  └────────────────────────────────────────┘  │
│              │                                              │
│              │  ┌────────────────────────────────────────┐  │
│              │  │ [输入框]  📎 📁  [发送]                 │  │
│              │  └────────────────────────────────────────┘  │
├──────────────┴───────────────────────────────────────────────┤
│ 文件浏览器  [Path: /pm-proposal/]                             │
│ 📄 requirements.docx (starter)  📄 proposal-draft.md (AI生成) │
└───────────────────────────────────────────────────────────────┘
```

**Key differences from generic chat:**
1. **Task-first navigation** — left sidebar shows training tasks, not free-form conversations
2. **Template-driven** — starting a new task loads the training template with description, steps, and starter files
3. **Submit button** — trainees can submit their work for instructor review
4. **Role-filtered** — trainees only see templates for their assigned role

### 7.2 Instructor/Admin View

```
┌──────────────────────────────────────────────────────────────┐
│ 嘉骏 AI 培训平台  [💬 聊天] [👥 学员] [🤖 Agents] [📊 看板]  │
├──────────────────────────────────────────────────────────────┤
│ 📊 培训看板                                                   │
│                                                              │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│ │ 在线学员  │ │ 已完成   │ │ 进行中   │ │ 平均分   │         │
│ │   28/30  │ │   12     │ │   18     │ │   78     │         │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘         │
│                                                              │
│ 各小组进度：                                                  │
│ ┌──────┬──────┬──────────┬──────────┬────────┐               │
│ │ 组号 │ 角色 │ 学员     │ 任务进度 │ 提交数 │               │
│ ├──────┼──────┼──────────┼──────────┼────────┤               │
│ │ 1-4  │ PM   │ 8 人     │ ████░░ 67│ 3      │               │
│ │ 5-8  │ 开发 │ 10 人    │ █████░ 83│ 5      │               │
│ │ 9-11 │ 硬件 │ 6 人     │ ██░░░░ 33│ 1      │               │
│ │ 12-15│ 销售 │ 6 人     │ ███░░░ 50│ 3      │               │
│ └──────┴──────┴──────────┴──────────┴────────┘               │
│                                                              │
│ 待评分作业：[张三 - 项目提案] [李四 - 代码] [王五 - 8D报告]   │
└──────────────────────────────────────────────────────────────┘
```

**Instructor capabilities:**
1. **Dashboard**: real-time view of trainee activity, progress by group, submission counts
2. **User management**: assign roles/groups, enable/disable accounts
3. **Agent management**: create agents, monitor status, restart/offline
4. **Submission review**: view submitted work, score (0-100), leave feedback
5. **Live monitoring**: see which conversations are active, which agents are busy

### 7.3 Tech Stack

- Pure HTML + Alpine.js + Tailwind CSS (zero build step, consistent with existing `/ui/`)
- Native `WebSocket` API for real-time communication
- Drag & Drop API + `webkitGetAsEntry()` for directory uploads
- `highlight.js` CDN for code block syntax highlighting
- Marked.js for Markdown rendering of agent responses

### 7.4 Interaction Details

1. **Task selection**: trainee picks a template → conversation created with template context + starter files copied to workspace
2. **Agent picker**: dropdown showing only `status=available` agents from `GET /chat/agents/available`
3. **Message sending**: input clears on send, shows loading indicator until `agent_status: thinking`
4. **Markdown rendering**: agent responses rendered as Markdown with code highlighting
5. **File browser**: tree view of user workspace, starter files marked with badge
6. **Submit work**: trainee clicks `[提交作业]` → conversation marked `submitted` → appears in instructor queue

## 8. Security

1. **Path isolation**: all file operations restricted to `/data/user-workspaces/{user_id}/`. Reject `..` and absolute paths.
2. **JWT domain allowlist**: reuse `AGENT_HUB_CF_ACCESS_ALLOWED_DOMAINS`.
3. **File size limits**: configurable via `AGENT_HUB_CHAT_MAX_FILE_SIZE` (default 10 MiB) and `AGENT_HUB_CHAT_MAX_DIR_SIZE` (default 50 MiB).
4. **Rate limiting**: 30 messages/minute/user (Phase 2 for finer control).
5. **Agent isolation**: agents only access the workspace of the task they are assigned.
6. **Admin gate**: `/admin/*` routes check email against `AGENT_HUB_ADMIN_EMAILS`, return 403 otherwise.
7. **Submission integrity**: submitted conversations are read-only for trainees (cannot modify after submission).

## 9. Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `AGENT_HUB_CHAT_WORKSPACE_ROOT` | `/data/user-workspaces` | Root directory for user workspaces |
| `AGENT_HUB_CHAT_TEMPLATE_ROOT` | `/data/training-templates` | Root directory for training template starter files |
| `AGENT_HUB_CHAT_MAX_FILE_SIZE` | `10485760` (10 MiB) | Max single file upload size |
| `AGENT_HUB_CHAT_MAX_DIR_SIZE` | `52428800` (50 MiB) | Max directory upload size |
| `AGENT_HUB_CHAT_CONTEXT_MESSAGES` | `5` | Number of recent messages sent to agent as context |
| `AGENT_HUB_ADMIN_EMAILS` | (empty) | Comma-separated admin emails |
| `AGENT_HUB_TRAINING_MODE` | `false` | When `true`, enables training-specific features (templates, submissions, scoring) |

## 10. Training-Specific Features

### 10.1 Skill Template Injection

When a trainee starts a task from a template, the agent receives the skill template as part of the task payload:

```json
{
  "task": "帮我写一份智能工厂改造项目提案",
  "skill_context": "你是资深项目经理。工作流：接到需求 → 查历史 → 生成方案 → 输出格式...",
  "files": [...],
  "workdir": "/data/user-workspaces/{user_id}/pm-proposal/"
}
```

This ensures the agent behaves according to the training curriculum's role definitions.

### 10.2 Starter File Copy

When a trainee starts a task:
1. Hub copies starter files from `/data/training-templates/{template_id}/` to `/data/user-workspaces/{user_id}/{template_id}/`
2. Creates a conversation linked to the template
3. First message is a system message with the task description

### 10.3 Submission Flow

1. Trainee clicks `[提交作业]` on a conversation
2. Hub marks conversation `status=submitted`, creates a `submissions` record
3. Conversation becomes read-only for the trainee
4. Instructor sees it in the submission queue at `/admin/chat/submissions`
5. Instructor reviews, scores (0-100), leaves feedback
6. Trainee sees their score and feedback

## 11. Out of Scope (Future Phases)

- Streaming/token-by-token agent responses
- Multi-agent group chats
- File sharing between users
- Voice/image input
- Conversation search/full-text indexing
- Fine-grained rate limiting per endpoint
- Automated scoring (AI evaluates submissions)
- Training analytics export (CSV/PDF reports)
