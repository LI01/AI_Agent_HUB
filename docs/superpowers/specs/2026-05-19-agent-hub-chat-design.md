# Design: Agent Hub Chat — User-to-Agent Conversational Interface

**Date:** 2026-05-19
**Status:** Draft
**Author:** Brainstorming Session

## 1. Overview

Add a conversational web interface to the Agent Hub that allows authenticated users to chat directly with individual agents in real time. The system supports text messages, file/directory uploads (including large files), and persistent conversation history per user. Admin users additionally get a management panel for users and agents.

**Approach:** Scheme A — lightweight extension of the existing Hub, reusing the task system, WebSocket infrastructure, and Cloudflare Access JWT authentication.

## 2. Authentication & User Model

### 2.1 Auth Flow

1. User visits `/chat` — Cloudflare Access intercepts, requires login.
2. After login, Access forwards `Cf-Access-Jwt-Assertion` header to Hub.
3. Hub validates JWT (reuses existing `verify_cf_access_jwt`), checks email domain against `AGENT_HUB_CF_ACCESS_ALLOWED_DOMAINS`.
4. Hub extracts `email` and `name` from JWT claims, upserts into `users` table.
5. Admin status: email checked against `AGENT_HUB_ADMIN_EMAILS` env var.

### 2.2 New Database Tables

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,              -- ULID
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    is_admin INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,              -- ULID
    user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT,                       -- Auto-generated: first 50 chars of first message
    agent_id TEXT NOT NULL,
    status TEXT DEFAULT 'active',     -- active / archived
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
```

### 2.3 Indexes

- `conversations(user_id, updated_at DESC)`
- `messages(conversation_id, created_at ASC)`
- `messages(task_id)`

## 3. User Workspace & File Storage

### 3.1 Directory Structure

Each user gets a private workspace:
```
/data/user-workspaces/{user_id}/
```

All file operations are scoped to this directory. Path traversal (`..`, absolute paths) is rejected.

### 3.2 File Upload

- **Small files** (≤ 10 MiB single, ≤ 50 MiB directory): sent inline via WebSocket as base64 in `chat_message.files`.
- **Large files**: `POST /chat/upload` with multipart chunked upload (5 MiB chunks).
- Files stored at `/data/user-workspaces/{user_id}/{path}`.
- Configurable limits: `AGENT_HUB_CHAT_MAX_FILE_SIZE` (default 10 MiB), `AGENT_HUB_CHAT_MAX_DIR_SIZE` (default 50 MiB).

### 3.3 File API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/chat/files?path=src/` | List directory contents |
| GET | `/chat/files?path=src/main.py` | Download file |
| POST | `/chat/upload` | Upload file/directory (chunked) |
| DELETE | `/chat/files?path=old.py` | Delete file |

### 3.4 Agent Access

When a chat task is dispatched to an agent, the task payload includes:
```json
{
  "workdir": "/data/user-workspaces/{user_id}/",
  "files": [...]
}
```
The agent can read/write files in this directory. Results are read back into the conversation.

## 4. WebSocket Protocol

### 4.1 Endpoint

`WS /chat/ws?token=<cf-jwt>`

### 4.2 Client → Hub Messages

```json
// Send a chat message
{
  "type": "chat_message",
  "conversation_id": "conv-abc123",
  "content": "帮我看看这段代码",
  "files": [{"path": "src/main.py", "content_base64": "...", "size": 1024}]
}
```

### 4.3 Hub → Client Messages

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

### 4.4 Internal Flow

1. Hub receives `chat_message` → persists to `messages` (role=user).
2. Creates a task via existing `POST /tasks` flow, payload includes user message + files + recent conversation context (last 5 messages).
3. Task dispatched to selected agent via existing agent WebSocket.
4. Agent reports result → Hub persists to `messages` (role=agent) → pushes to client via chat WebSocket.

## 5. REST API

### 5.1 New Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/chat` | CF JWT | Chat landing page (HTML) |
| GET | `/chat/{conv_id}` | CF JWT | Single conversation page (HTML) |
| GET | `/chat/conversations` | CF JWT | List user's conversations |
| POST | `/chat/conversations` | CF JWT | Create new conversation |
| GET | `/chat/conversations/{id}/messages` | CF JWT | Get conversation history |
| POST | `/chat/upload` | CF JWT | File/directory upload |
| GET | `/chat/files` | CF JWT | Browse user workspace |
| GET | `/chat/files/*` | CF JWT | Download file |
| DELETE | `/chat/files/*` | CF JWT | Delete file |
| GET | `/chat/agents/available` | CF JWT | List available agents |

### 5.2 Admin Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/admin/chat/users` | Admin JWT | List all users |
| POST | `/admin/chat/users/{id}/disable` | Admin JWT | Disable user |
| POST | `/admin/chat/users/{id}/enable` | Admin JWT | Enable user |
| GET | `/admin/chat/users/{id}/conversations` | Admin JWT | View user's conversations (read-only) |
| GET | `/admin/chat/agents` | Admin JWT | List all agents with status |
| POST | `/admin/chat/agents` | Admin JWT | Create new agent |
| POST | `/admin/chat/agents/{id}/restart` | Admin JWT | Restart agent |
| POST | `/admin/chat/agents/{id}/offline` | Admin JWT | Force agent offline |
| DELETE | `/admin/chat/agents/{id}` | Admin JWT | Delete agent |

### 5.3 Agent Creation (`POST /admin/chat/agents`)

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

## 6. Frontend Design

### 6.1 Layout

```
┌─────────────────────────────────────────────────┐
│ Agent Hub Chat  [💬] [👥 Admin] [🤖 Admin] [👤] │
├──────────────┬──────────────────────────────────┤
│ Conversations│  Current Conversation             │
│              │                                  │
│ [+ New Chat] │  ┌────────────────────────────┐  │
│ ● Claude-    │  │ Agent: Hello, how can I    │  │
│   Coder      │  │ help?                      │  │
│   2h ago     │  └────────────────────────────┘  │
│              │  ┌────────────────────────────┐  │
│ ○ Codex-     │  │ User: Check this code      │  │
│   Reviewer   │  │ 📎 src/main.py             │  │
│   Yesterday  │  └────────────────────────────┘  │
│              │  ┌────────────────────────────┐  │
│              │  │ Agent: The issue is...     │  │
│              │  └────────────────────────────┘  │
│              │                                  │
│              │  ┌────────────────────────────┐  │
│              │  │ [Input]  📎 📁  [Send]     │  │
│              │  └────────────────────────────┘  │
├──────────────┴──────────────────────────────────┤
│ File Browser  [Path: /src/]                     │
│ 📁 src/  📄 main.py (1.2KB)  📄 utils.py       │
└─────────────────────────────────────────────────┘
```

### 6.2 Tech Stack

- Pure HTML + Alpine.js + Tailwind CSS (zero build step, consistent with existing `/ui/`)
- Native `WebSocket` API for real-time communication
- Drag & Drop API + `webkitGetAsEntry()` for directory uploads
- `highlight.js` CDN for code block syntax highlighting
- Marked.js for Markdown rendering of agent responses

### 6.3 Interaction Details

1. **Conversation list**: click to switch, `+ New Chat` opens agent picker.
2. **Agent picker**: dropdown showing only `status=available` agents from `GET /chat/agents/available`.
3. **Message sending**: input clears on send, shows loading indicator until `agent_status: thinking`.
4. **Markdown rendering**: agent responses rendered as Markdown with code highlighting.
5. **File browser**: tree view of user workspace, click to preview/download.

### 6.4 Admin Views

- **User management** (`/admin/users`): list, search, disable/enable, view conversations (read-only).
- **Agent management** (`/admin/agents`): status overview, create new agent (form → generates API key + install command), restart/offline/delete.
- Admin nav bar includes `[💬 Chat] [👥 Users] [🤖 Agents]` tabs.
- Non-admin users see only the chat interface.

## 7. Security

1. **Path isolation**: all file operations restricted to `/data/user-workspaces/{user_id}/`. Reject `..` and absolute paths.
2. **JWT domain allowlist**: reuse `AGENT_HUB_CF_ACCESS_ALLOWED_DOMAINS`.
3. **File size limits**: configurable via `AGENT_HUB_CHAT_MAX_FILE_SIZE` (default 10 MiB) and `AGENT_HUB_CHAT_MAX_DIR_SIZE` (default 50 MiB).
4. **Rate limiting**: 30 messages/minute/user (Phase 2 for finer control).
5. **Agent isolation**: agents only access the workspace of the task they are assigned.
6. **Admin gate**: `/admin/*` routes check email against `AGENT_HUB_ADMIN_EMAILS`, return 403 otherwise.

## 8. Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `AGENT_HUB_CHAT_WORKSPACE_ROOT` | `/data/user-workspaces` | Root directory for user workspaces |
| `AGENT_HUB_CHAT_MAX_FILE_SIZE` | `10485760` (10 MiB) | Max single file upload size |
| `AGENT_HUB_CHAT_MAX_DIR_SIZE` | `52428800` (50 MiB) | Max directory upload size |
| `AGENT_HUB_CHAT_CONTEXT_MESSAGES` | `5` | Number of recent messages sent to agent as context |
| `AGENT_HUB_ADMIN_EMAILS` | (empty) | Comma-separated admin emails |

## 9. Out of Scope (Future Phases)

- Streaming/token-by-token agent responses
- Multi-agent group chats
- File sharing between users
- Voice/image input
- Conversation search/full-text indexing
- Fine-grained rate limiting per endpoint
