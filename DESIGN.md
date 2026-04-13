
# KoreData
Folder C:\Util\GithubRepos\KoreData defines a data source service, that scrapes the internet for new data, or holds a wikipedia clone. It provides that data to an agent to support its activities.

# MiniAgentFramework
Folder C:\Util\GithubRepos\MiniAgentFramework defines an agent framework that wraps an LLM in context and orchestration functionality to perform actions beyond the scope of a simple chat interface.

# KoreComms

## Purpose

KoreComms interfaces external communication services with MiniAgentFramework, allowing the agent to send and receive messages beyond a single PC. It acts as a central communication hub: normalising messages from heterogeneous sources into a single, sequentially-processed queue, and routing replies back out via the correct channel.

---

## System Context

KoreComms is one of three co-operating services:

| Service | Role |
|---|---|
| **KoreData** | Provides data to the agent (web scraping, Wikipedia clone) |
| **MiniAgentFramework** | LLM wrapper with context and orchestration |
| **KoreComms** | External communication hub (this service) |

All services run locally (same machine by default) but are bound to an IP address so they can be re-hosted or distributed in future.

---

## Tech Stack

- **Language / Framework:** Python — FastAPI or Flask, consistent with KoreData and MiniAgentFramework.
- **Database:** SQLite, held locally, storing all conversations and messages.
- **Deployment:** Local, IP-bound. Mirrors KoreData's hosting model so the service address is configurable.

---

## Core Concepts

### Interfaces
An *interface* represents a single connection to an external (or internal) communication channel. The design supports many interface *types* and multiple *instances* of each type. In the first implementation there is one Gmail account and one Manual interface.

| Interface Type | Description | First Instance |
|---|---|---|
| **Gmail** | Polls a Gmail mailbox via the Gmail API | One OAuth2 account |
| **Manual** | Synthetic channel for local testing; messages injected via WebUI, replies stored internally | One instance |

All future interface types (e.g. Outlook, SMS, Slack) must conform to the same interface contract so they can be added without restructuring the core.

### Thread-Safe Message Queue
All interface instances feed received messages into **one shared, thread-safe queue**. Messages are processed **sequentially** (FIFO). This ensures the agent never handles two messages concurrently and simplifies state management.

### Conversations & Threading
Messages are grouped into **conversations** (e.g. an email reply chain maps to one conversation). When the agent requests the next message, it receives the full conversation thread — all prior messages in that conversation — so it has complete context.

---

## Message Lifecycle

```
[External Source]
      │
      ▼
  RECEIVED          ← message arrives from an interface (Gmail poll or Manual injection)
      │
      ▼
   QUEUED           ← placed on the thread-safe inbound queue
      │
      ▼
  PROCESSING        ← agent has called GET /next-message; message locked for agent
      │
      ▼
  HANDLED
   ├── Replied      ← agent called POST /reply, then POST /complete
   └── Ignored      ← agent called POST /complete without replying
```

- Only one message is in PROCESSING at a time (single agent, single KoreComms).
- Once HANDLED, the agent calls GET /next-message again to advance.

---

## Agent REST API

The agent (MiniAgentFramework) communicates with KoreComms exclusively via REST.

| Endpoint | Method | Description |
|---|---|---|
| `/next-message` | GET | Dequeue the next QUEUED message. Returns the message plus full conversation thread. Moves state to PROCESSING. Returns 204 if queue is empty. |
| `/reply` | POST | Send a reply via the same channel the inbound message arrived on. Body includes message ID and reply text/content. |
| `/complete` | POST | Mark the current message as HANDLED. Body specifies sub-state: `replied` or `ignored`. |
| `/send` | POST | Initiate a brand-new outbound message on a specified interface (agent or human-triggered). |

All endpoints return JSON. The agent and WebUI are the only consumers of this API.

---

## Interface: Gmail

- **Authentication:** OAuth2 with a stored refresh token (standard Gmail API). Credentials configured via the WebUI and persisted in the database.
- **Polling:** Background thread polls at a configurable interval (default **60 seconds**). Interval is stored in configuration and adjustable via the WebUI.
- **Inbound:** New emails are fetched, de-duplicated (tracked by Gmail message ID), parsed, and inserted into the message queue.
- **Threading:** Gmail thread IDs are used to group messages into KoreComms conversations.
- **Outbound / Reply:** Replies are sent via the Gmail API using the same thread. New outbound messages create a new Gmail thread.

---

## Interface: Manual (Testing)

- Messages are injected by a human through the WebUI (Compose / Inject form).
- The injected message is placed on the shared queue exactly as if it arrived from an external source.
- Replies from the agent are stored in the database and visible in the WebUI — no external transmission occurs.
- This interface is always present and cannot be removed; it provides a zero-dependency test path.

---

## WebUI

Mirrors the KoreData WebUI pattern (locally accessible web interface). Provides:

1. **Message View** — browse all incoming and outgoing messages across all interfaces, with conversation threading.
2. **Compose / Inject** — create a synthetic inbound message via the Manual interface to test agent behaviour.
3. **Connection Configuration** — add, edit, or remove external interface connections (e.g. add a Gmail OAuth account, set polling interval).
4. **State Editor** — view and manually override message state (for debugging, e.g. re-queue a HANDLED message).
5. **Agent Activity Log** — view a log of agent actions: messages fetched, replies sent, completions recorded.

---

## Database Schema (outline)

| Table | Key Columns |
|---|---|
| `interfaces` | id, type, name, config_json, enabled |
| `conversations` | id, interface_id, external_thread_id, created_at |
| `messages` | id, conversation_id, direction (inbound/outbound), status, content, received_at, handled_at |
| `config` | key, value (for global settings such as polling interval) |

- `config_json` on `interfaces` stores interface-specific credentials and settings (e.g. Gmail refresh token, poll interval override). Sensitive values should be stored encrypted at rest.

---

## Non-Functional Requirements

- **Sequential processing:** the inbound queue is consumed one message at a time; no concurrent agent processing.
- **Reliability:** if polling fails (network error, API quota), log the error and retry on the next poll cycle; do not crash.
- **Security:** OAuth2 refresh tokens and any credentials stored in the database must be encrypted at rest. The REST API should be accessible only on localhost (or a trusted network) — no public exposure without additional auth.
- **Extensibility:** adding a new interface type requires implementing a defined Python interface (adapter pattern) and registering it; no changes to core queue or agent API.
- **Consistency with ecosystem:** coding style, project structure, and configuration conventions should match KoreData and MiniAgentFramework.

