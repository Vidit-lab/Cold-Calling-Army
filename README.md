<div align="center">

# 📞 Cold-Calling-Army

### Autonomous outbound voice agent that dials real phone numbers, holds a live spoken conversation with Gemini, and books appointments — end to end.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![LiveKit](https://img.shields.io/badge/LiveKit-Agents%201.x-FF6B00)
![Gemini](https://img.shields.io/badge/Gemini-Live%20(native%20audio)-10B981?logo=googlegemini&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Uvicorn-009688?logo=fastapi&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-Postgres-3ECF8E?logo=supabase&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Coolify-2496ED?logo=docker&logoColor=white)

</div>

---

## 1. 📖 Overview

**Cold-Calling-Army** (internally *OutboundAI*) is a single-operator platform for running **autonomous outbound phone campaigns**. You upload leads or trigger a call from a dashboard; the system places a real PSTN call, and once the lead answers, a **Gemini Live** voice agent conducts the conversation natively (speech-in / speech-out), qualifies the lead, checks availability, and books an appointment — invoking real backend tools as it talks.

Every call is recorded, every outcome is logged, and every call's **Gemini token cost** is estimated and stored, so the dashboard shows booking rate, call outcomes, durations, and spend.

**🎯 Primary use cases**
- Appointment setting and lead qualification at volume
- Reminder / callback campaigns on a daily or weekday schedule
- Any outbound flow where a human script would otherwise be read line-by-line

**💡 Core value proposition** — it replaces the repetitive first-touch outbound call with an AI agent that is *observable* (full logs + recordings), *cost-aware* (per-call USD estimate), and *configurable* (swap the agent's voice, model, prompt, and enabled tools per campaign without a redeploy). It is built as a small, honest system: one container, managed cloud dependencies, and a clear set of reliability mechanisms earned from real failure modes.

> ⚠️ **Scope honesty:** this is a working MVP. It is single-tenant (no auth layer), uses logical (FK-free) joins, and tracks Gemini cost only (carrier/LiveKit minutes are separate). The **Future Improvements** section below is explicit about these boundaries.

---

## 2. 🎬 Quick Video Demo

A full walkthrough — dispatching a call, the live conversation, booking, recording playback, and the analytics dashboard.

▶️ **[`Demo Video/DemoVideo.mp4`](Demo%20Video/DemoVideo.mp4)**

> The clip is committed as a local asset. For inline playback on GitHub, drag the file into the README via the web editor (GitHub re-hosts it) or attach it to a Release.

### 🖥️ Dashboard Preview

| | |
|:---:|:---:|
| ![Dashboard preview 1](DashBoardPreview/Screenshot%20from%202026-06-14%2009-12-46.png) | ![Dashboard preview 2](DashBoardPreview/Screenshot%20from%202026-06-14%2009-13-11.png) |
| ![Dashboard preview 3](DashBoardPreview/Screenshot%20from%202026-06-14%2009-13-21.png) | ![Dashboard preview 4](DashBoardPreview/Screenshot%20from%202026-06-14%2009-14-17.png) |

> 💡 **Want the full preview?** Copy the [`ui/index.html`](ui/index.html) file and paste it into any HTML viewer.

---

## 3. 🏗️ Architecture Overview

The system is a **dispatch-and-converse** design. The API server never speaks to the lead — it mints a LiveKit room and a job; a separate worker process owns the actual call. One job = one room = one phone call.

![Voice Agent Architecture](Design%20Diagrams/VoiceAgentArchitecture.png)

**🧩 Major subsystems**

| Subsystem | Responsibility |
|-----------|----------------|
| **API Server** (`server.py`) | FastAPI dashboard + REST API; dispatches calls and schedules campaigns |
| **Agent Worker** (`agent.py`) | Per-call LiveKit agent; dials, runs the AI session, records, and logs |
| **Tool Layer** (`tools.py`) | Nine function tools the model invokes (book, check, lookup, remember, transfer, SMS, end…) |
| **Cost Engine** (`cost.py`) | Turns Gemini's per-modality token usage into a per-call USD estimate |
| **Data Layer** (`db.py`) | All Supabase reads/writes (sync client for bootstrap, async for the request path) |
| **AI** | Gemini Live (native audio) primary; Deepgram + Gemini 2.0 + Google TTS pipeline fallback |
| **Telephony** | LiveKit Cloud (media / SIP / dispatch / egress) bridged to a Vobiz SIP trunk → PSTN |

**🔄 Information flow (one call):** Dashboard → `POST /api/call` → LiveKit room + agent dispatch → Worker connects → **dials via Vobiz and waits for answer** → starts Gemini Live + recording → conversation loop with tool calls → writes appointment / SMS / Cal.com → `end_call` logs outcome, duration, recording URL, and cost to Supabase.

### 🗺️ The four key diagrams

#### 3.1 — 🎙️ Voice Agent Architecture *(shown above)*

- **Purpose:** the signal path and lifecycle of a single live call.
- **Components:** Lead (PSTN) ↔ Vobiz ↔ LiveKit Cloud ↔ Gemini Live ↔ Tool Layer ↔ Supabase.
- **Data flow:** audio is a closed loop (lead → carrier → LiveKit → Gemini → back), bidirectional **only after answer**; tool calls branch off Gemini and return short spoken strings.
- **Key tradeoff — the dial-first invariant:** the Gemini session starts *only after* `create_sip_participant(wait_until_answered=True)` returns. Starting during the 20–30 s ring would trip Gemini's idle timeout and silently kill the session before hello.

#### 3.2 — 🗂️ CRM & Tool Orchestration

![CRM Architecture](Design%20Diagrams/CRMArchitecture.png)

- **Purpose:** how Gemini's intent becomes action, and how a contact's record is assembled.
- **Components:** Gemini function calling → `AppointmentTools` (allow-list filter + per-call context) → tools grouped into Booking/Calendar, CRM/Memory, Comms/Lifecycle → Supabase, Cal.com, Twilio, LiveKit SIP.
- **Data flow:** the **Contact is virtual** — there is no `contacts` table; a contact is the set of `call_logs`, `appointments`, and `contact_memory` rows sharing a `phone_number`, reassembled at call start by `lookup_contact`.
- **Responsibilities:** tools execute side effects and report back; the model stays the single decision-maker. Memory self-prunes via a cheap Gemini 2.0 Flash pass once a contact exceeds five notes.

#### 3.3 — 🐳 Deployment Architecture

![Deployment Architecture](Design%20Diagrams/DeploymentArchitecture.png)

- **Purpose:** where the code physically runs.
- **Components:** one Docker container on Coolify running **two processes** — Uvicorn/FastAPI (foreground, the only listening port `:8000`, health-checked) and the Agent Worker (background, auto-restarting). All heavy lifting is managed cloud: LiveKit, Supabase (Postgres + S3), Gemini, Vobiz.
- **Data flow:** Coolify injects env and health-checks `:8000`; `start.sh` forks the worker into a 5-second restart loop, then `exec`s Uvicorn.
- **Key tradeoff:** monolithic-but-resilient — trivial to deploy, and a worker crash never flips the deployment unhealthy, at the cost of shared CPU/memory between dashboard and worker.

#### 3.4 — 📅 Appointment Booking Flow

![Booking Flow](Design%20Diagrams/BookingFlow.png)

- **Purpose:** the guard-then-commit path from a spoken "Tuesday at 3 works" to a confirmed row.
- **Components:** `check_availability` → `check_slot` (Supabase) → `book_appointment` (local INSERT, returns an 8-char booking ID) → optional `book_calcom` + `send_sms_confirmation`.
- **Data flow:** if the slot is taken, `get_next_available` walks forward in one-hour steps within 09:00–18:00 for up to seven days and offers an alternative — a conversational retry loop until the lead agrees or declines twice.
- **Key tradeoff:** the local `appointments` row *is* the slot lock; Cal.com and Twilio are best-effort post-effects that never block or fail the booking.

---

## 4. 🧠 Core Concepts & Design Decisions

Modern voice-AI system patterns that are actually implemented here:

### 🤖 Agent Runtime
A LiveKit Agent worker registered as `outbound-caller`. Each call is an isolated job with its own room, prompt, tool set, and in-memory context (`AppointmentTools`). **Why:** isolates call state per process and scales by adding worker slots.

### 🧰 Tool Orchestration
Nine tools attached to the **Agent** (not the session — a documented LiveKit 1.x footgun where session-level tools are silently dropped). An **allow-list** (`build_tool_list`) gates which tools a given agent profile may use. **Why:** one codebase becomes many "agents" by toggling tools + prompt as data, not code.

### 🔁 Call State Machine
A per-call lifecycle that converges to a logged terminal state: `INIT → CONNECTING → DIALING → ANSWERED → SESSION_START → (LISTEN ⇄ THINK ⇄ SPEAK / TOOL) → ENDING → LOGGING → COMPLETED`. **Why:** guarantees no call ends untracked — a safety net writes a `call_log` row even if the model forgets to call `end_call`.

### 💰 Cost Attribution Engine
`cost.py` folds Gemini's own `usageMetadata` (surfaced via LiveKit's `metrics_collected` event) into per-modality token totals, then prices them against a published table — keeping the audio/text split because audio output ($12/1M) dwarfs text. **Why:** live per-call cost visibility; it's an *estimate*, reconciled monthly against Google billing.

### ⚡ Event-Driven Hooks
The worker reacts to events rather than polling: `metrics_collected` (cost), `participant_disconnected` for the specific SIP identity (clean teardown), and agent dispatch from the API server. **Why:** decouples the conversation loop from lifecycle bookkeeping.

### 🔗 CRM Synchronization
A phone-keyed, schema-on-read CRM: writes accrue across `call_logs` / `appointments` / `contact_memory`; reads reassemble the contact on the next call. **Why:** the agent "remembers" a lead across calls without a dedicated contacts table.

### 💭 Conversational Memory
`remember_details` persists free-text insights; a Gemini 2.0 Flash pass compresses them to 3–5 bullets past five notes. **Why:** bounds context growth and keeps the expensive realtime model's prompt lean.

### 🔭 Observability Pipeline
A stdout-first, Supabase-backed log spine (`error_logs`), a worker **boot marker** that proves which build is live, and post-call **egress status polling** that surfaces silent S3 upload failures. **Why:** debuggable in production without external tooling (tracing/metrics/alerting are explicit future work).

### 🤝 Human Handoff Layer
`transfer_to_human` performs a SIP `REFER` to a configured fallback number. **Why:** graceful escalation when automation shouldn't continue (angry lead, complex request, explicit ask).

### 🛡️ Reliability Mechanisms
Dial-first ordering, transparent session resumption, context-window compression, low end-of-speech VAD sensitivity, *no* `close_on_disconnect` (SIP blips ≠ hangup), a `MAX_CALL_SECONDS` hard cap that deletes the room to drop the carrier leg, a realtime→pipeline fallback, and a worker auto-restart loop. **Why:** each one exists because a specific failure mode was observed in production.

---

## 5. 📂 Repository Structure

```
Cold-Calling-Army/
├── agent.py              # LiveKit voice-agent worker — per-call entrypoint, dial, session, recording, logging
├── server.py             # FastAPI dashboard + REST API + APScheduler campaign engine
├── tools.py              # AppointmentTools — the 9 function tools exposed to Gemini
├── cost.py               # Per-modality Gemini token → USD cost engine
├── db.py                 # Supabase data-access layer (sync bootstrap + async request path)
├── prompts.py            # "Priya" persona + system-prompt builder
├── diagnose_db.py        # Standalone Supabase round-trip diagnostic
├── supabase_schema.sql   # 7-table schema; idempotent, expand-only migrations
├── requirements.txt      # Python dependencies
├── Dockerfile            # python:3.11-slim image
├── start.sh              # Launches worker (bg, auto-restart) + uvicorn (fg, :8000)
├── .env.example          # All configuration keys with placeholders
├── ui/
│   └── index.html        # Single-file dashboard SPA (vanilla JS + Chart.js)
├── Design Diagrams/      # Rendered architecture diagrams (PNG)
└── Demo Video/           # Product walkthrough
```

**Organization strategy:** a deliberately flat, single-package layout. Each top-level module owns one concern (orchestration, telephony agent, tools, cost, data), which keeps the dependency graph shallow and the system easy to read in one sitting. The dashboard is a single static HTML file served directly by FastAPI — no build step.

---

## 6. 🛠️ Technology Stack

| Layer | Technology | Purpose & architectural significance |
|-------|------------|--------------------------------------|
| Language | **Python 3.11** | Async-first; matches the LiveKit Agents and Supabase async clients |
| Voice agent runtime | **LiveKit Agents 1.x** | Per-call job model, SIP bridging, egress recording, agent dispatch — the backbone of the call lifecycle |
| Realtime AI | **Google Gemini Live** (`gemini-3.1-flash-live-preview`, voice `Aoede`) | Native audio-to-audio with function calling — the conversational brain; chosen for low-latency speech + tool use in one model |
| Fallback AI | **Deepgram `nova-3` + Gemini 2.0 Flash + Google TTS + Silero VAD** | Degraded STT→LLM→TTS pipeline when realtime is unavailable |
| Telephony / media | **LiveKit Cloud** | Managed media server, SIP↔WebRTC bridge, recording egress — removes all media-plane ops |
| Carrier | **Vobiz SIP trunk** | PSTN origination for outbound calls |
| API / backend | **FastAPI + Uvicorn** | REST API, dashboard host, and call dispatcher; single listening port `:8000` |
| Scheduling | **APScheduler** | In-process cron for `once` / `daily` / `weekdays` campaigns |
| Database | **Supabase (Postgres)** via `supabase-py` | System of record (7 tables) *and* runtime config store (`settings`) |
| Object storage | **Supabase S3-compatible storage** | OGG call recordings uploaded by LiveKit egress (`force_path_style`) |
| Calendar | **Cal.com REST API** | External booking sync (optional, best-effort) |
| SMS | **Twilio** | Booking confirmation texts (optional, silent-skip if unconfigured) |
| Frontend | **Vanilla JS + Chart.js 4.4.3** | Zero-build single-file dashboard: stats, dialing, campaigns, agent profiles, CRM, logs |
| Packaging / deploy | **Docker + Coolify** | One image, two processes; Coolify injects env and health-checks `:8000` |

---

## 7. ⚙️ Setup & Installation

### 📋 Prerequisites
- **Python 3.11**
- Accounts / credentials for: **LiveKit Cloud**, **Google AI (Gemini)**, **Supabase**, and a **Vobiz** SIP trunk
- Optional: **Cal.com**, **Twilio**, **Deepgram** (each degrades gracefully if absent)

### 📥 Install
```bash
git clone https://github.com/Vidit-lab/Cold-Calling-Army.git
cd Cold-Calling-Army
pip install -r requirements.txt
cp .env.example .env        # then fill in your credentials
```

### 🗄️ Initialize the database
Run [`supabase_schema.sql`](supabase_schema.sql) in **Supabase → SQL Editor**. It is idempotent (safe to re-run). Verify connectivity any time with:
```bash
python diagnose_db.py
```

### ☎️ Create the SIP trunk
With LiveKit + Vobiz credentials set, create the outbound trunk once (stores `OUTBOUND_TRUNK_ID`):
```bash
curl -X POST http://localhost:8000/api/setup/trunk
```
(or use the **Setup** tab in the dashboard).

### 💻 Local development
Run the two processes separately:
```bash
# Terminal 1 — dashboard + API (hot reload)
uvicorn server:app --reload --port 8000

# Terminal 2 — voice-agent worker (LiveKit Agents dev mode)
python agent.py dev
```
Open **http://localhost:8000**.

### 🚢 Production
The container runs both processes via `start.sh` (worker in the background with auto-restart, Uvicorn in the foreground):
```bash
docker build -t outboundai .
docker run -p 8000:8000 --env-file .env outboundai
```
On **Coolify**, point it at the repo, inject the environment variables, and let it health-check port `8000`.

---

## 8. 🔧 Configuration

All configuration is environment-driven (see [`.env.example`](.env.example)). The `settings` table can override most non-bootstrap keys at runtime; agent profiles and `ENABLED_TOOLS` act as per-call feature toggles.

| Group | Keys | Notes |
|-------|------|-------|
| **LiveKit** | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Media/SIP/dispatch credentials |
| **Gemini** | `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `USE_GEMINI_REALTIME` | Defaults: `gemini-3.1-flash-live-preview`, voice `Aoede`, realtime on |
| **Vobiz / SIP** | `VOBIZ_SIP_DOMAIN`, `VOBIZ_USERNAME`, `VOBIZ_PASSWORD`, `VOBIZ_OUTBOUND_NUMBER`, `OUTBOUND_TRUNK_ID`, `DEFAULT_TRANSFER_NUMBER` | Carrier + human-handoff target |
| **Supabase** | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | **Protected bootstrap keys** — never overridden by the `settings` table |
| **Storage (S3)** | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET` | Recording uploads (optional) |
| **Cal.com** | `CALCOM_API_KEY`, `CALCOM_EVENT_TYPE_ID`, `CALCOM_TIMEZONE` | Calendar sync (optional) |
| **Twilio** | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | SMS confirmations (optional) |
| **Deepgram** | `DEEPGRAM_API_KEY` | Fallback-pipeline STT (optional) |
| **Limits** | `MAX_CALL_SECONDS` | Hard cap per call; defaults to `300` if unset |

**Secrets handling:** `.env` is git-ignored and docker-ignored; in production, Coolify injects secrets at runtime. Sensitive keys (API secrets, service keys, passwords) are never returned in plaintext by the settings API.

**Runtime configuration & feature flags:**
- **`settings` table** — runtime overrides hydrated into the worker's environment at boot (protected DB credentials excluded).
- **Agent profiles** — named presets (`voice`, `model`, `system_prompt`, `enabled_tools`, `is_default`) selectable per call or per campaign.
- **`ENABLED_TOOLS`** — JSON allow-list gating which of the nine tools a call may use.

---

## 9. 🚀 Future Improvements

**📈 Scalability**
- Split the dashboard and worker into independent deployments; run a worker pool keyed off LiveKit dispatch for parallel campaigns.
- Add Postgres connection pooling (Supavisor) and indexes on `call_logs(phone_number, timestamp)` and `appointments(date, time, status)`.
- Push `get_stats` aggregation into SQL (views / `GROUP BY`) and pre-aggregate daily rollups to stop full-table scans on every poll.

**🧯 Reliability**
- Add a unique constraint on `appointments(date, time)` (partial, `status='booked'`) to close the double-book race; make `book_appointment` idempotent.
- Introduce a dead-letter path for failed dials so the funnel is complete and re-dialable.
- Deepen the health check to verify worker heartbeat + DB reachability (today only `:8000` liveness is checked).

**✨ AI capabilities**
- Add `agent_profile_id` / `campaign_id` to `call_logs` to enable per-agent and per-campaign A/B analysis (the current star-schema gap).
- Persist `calcom_booking_uid` on the appointment row so cross-call Cal.com cancellation works.
- Externalize the cost price table and split per-modality token counts into a dedicated cost table for finance-grade reporting.

**📟 Operational**
- Add correlation/trace IDs across server → LiveKit → worker → Gemini, a metrics exporter (Prometheus), log retention, and alerting on dial-failure and worker restart-loop rates.
- Move timestamps to `timestamptz` / UTC.
- Introduce auth + Supabase RLS and an `org_id` for multi-tenancy.

---

## 10. 🙏 Acknowledgements

Built on the work of:

- **[LiveKit](https://livekit.io/)** — agents framework, media server, SIP, and egress
- **[Google Gemini](https://ai.google.dev/)** — Gemini Live (native audio) and Gemini 2.0 Flash
- **[Supabase](https://supabase.com/)** — Postgres and S3-compatible storage
- **[Vobiz](https://vobiz.ai/)** — SIP trunking / PSTN origination
- **[Cal.com](https://cal.com/)** — calendar booking
- **[Twilio](https://twilio.com/)** — SMS
- **[Deepgram](https://deepgram.com/)** — speech-to-text (fallback pipeline)
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[APScheduler](https://apscheduler.readthedocs.io/)**, **[Chart.js](https://www.chartjs.org/)**, and **[Coolify](https://coolify.io/)**

---

<div align="center">

**Made by Vidit**

</div>
