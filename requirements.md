# LocalClip — Requirements (v1)

## 1. Overview

LocalClip is a local-first web clipper that captures ChatGPT conversations turn by turn,
processes each closed turn with a local LLM (Ollama), and writes an Obsidian-compatible
markdown vault where every turn is a node in a linked conversation graph. No data ever
leaves the machine. Built for a researcher who loses insights across long AI conversations.

---

## 2. Functional Requirements

### 2.1 Browser Extension (Tampermonkey Userscript)

#### FR-US-01 — Supported sites (v1)
The script is active on `chatgpt.com` and `chat.openai.com` only.
The `SITE_ADAPTERS` structure is retained for future expansion; Claude and Gemini entries
are present as disabled stubs with TODO comments.

#### FR-US-02 — Blocklist
The script must never capture on pages whose hostname contains any substring from the
blocklist. Default entries: `bank`, `paypal`, `mail.google.com`, `outlook`, `health`,
`medical`, `login`, `accounts.google.com`. Editable as a constant in the script.

#### FR-US-03 — ChatGPT adapter (verified selectors)
The ChatGPT adapter is defined in `SITE_ADAPTERS["chatgpt.com"]` (also used for
`chat.openai.com`) with the following verified selectors. **Do not use hashed or Tailwind
class names anywhere in this adapter.**

| Field | Value | Verification |
|-------|-------|-------------|
| `turnSelector` | `section[data-testid^="conversation-turn-"]` | Confirmed via DevTools on live ChatGPT conversation — every turn is a `<section>` with this testid prefix |
| `roleAttr` | `dataset.turn` values `"user"` / `"assistant"` | Same section element carries `data-turn` attribute |
| `messageSelector` | `[data-message-author-role]` | Descendant of the turn section; multiple per turn are joined |
| `messageIdAttr` | `data-message-id` on first message element in turn | Falls back to sha256(role + "\|" + normalizedText) if absent |
| `conversationIdPattern` | `/\/c\/([0-9a-f-]{36})/` on `location.pathname` | URL path on chatgpt.com for named conversations |

Assistant rendering: two renderers exist in the DOM — `.markdown` class and a widget
renderer using `p[data-d-component="text"]`. The adapter must **not** depend on
`.markdown`. Extraction procedure per turn:
1. Clone the message element (never mutate live DOM).
2. Strip from clone: `button`, `svg`, `.sr-only`, `[role="group"]`, `[role="separator"]`.
3. Run Turndown on the cleaned clone's `innerHTML`.
4. If Turndown result is empty or whitespace-only, fall back to `element.innerText`.

#### FR-US-04 — Conversation ID gating
If `location.pathname` does not match `/\/c\/([0-9a-f-]{36})/` (i.e. a new chat with no
ID yet), skip auto-polling. Begin polling only after the URL acquires a conversation ID.

#### FR-US-05 — Polling loop (replaces MutationObserver / idle debounce)
Every 5 seconds the script:
1. Reads all `section[data-testid^="conversation-turn-"]` elements in DOM order.
2. Pairs each user turn with the immediately following assistant turn.
3. Counts the number of user turns.
4. If the count has increased since the last successful POST, POSTs the full ordered list
   of `{id, role, markdown}` for the entire conversation.

There is no MutationObserver, no idle debounce, no stop-button detection, and no
render-state attribute checking.

#### FR-US-06 — Manual flush (`Alt+S`)
`Alt+S` sends immediately with `flush: true` in the payload. The server processes all
turns including the last (pending) one when `flush: true`.

#### FR-US-07 — `Alt+P` pause toggle
`Alt+P` toggles the polling pause state and shows a toast: "Auto-capture paused" or
"Auto-capture resumed".

#### FR-US-08 — DOM safety
Always clone nodes before cleaning. Never mutate the live page.

#### FR-US-09 — Turndown configuration
Must preserve: fenced code blocks with language hints, ordered/unordered lists, tables,
and hyperlinks.

#### FR-US-10 — Message shape
Each turn produces one object:
```json
{ "id": "<data-message-id or sha256 fallback>", "role": "user|assistant", "markdown": "..." }
```
Text normalization (trim, collapse whitespace) is applied before hashing.

#### FR-US-11 — Payload schema
```json
{
  "url":             "https://chatgpt.com/c/<uuid>",
  "title":           "<document.title>",
  "source":          "chat",
  "conversation_id": "<uuid>",
  "messages":        [{ "id": "...", "role": "...", "markdown": "..." }, ...],
  "flush":           false,
  "captured_at":     "<ISO-8601>"
}
```

#### FR-US-12 — Authentication header
Every request includes `X-LocalClip-Token: <token>` (value pasted into script constant).

#### FR-US-13 — Toast notifications
Small fixed-position overlay (bottom-right, z-index 999999), auto-dismiss after 3 s.
Messages: `saved N turns`, `skipped`, `duplicate`, `error: <message>`.

#### FR-US-14 — CDN dependencies (pinned)
```
// @require https://cdn.jsdelivr.net/npm/@mozilla/readability@0.6.0/Readability.js
// @require https://cdn.jsdelivr.net/npm/turndown@7.2.4/lib/turndown.browser.umd.js
```
(Readability is kept for future article-clipping support; not used in v1 chat flow.)

---

### 2.2 Server

#### FR-SV-01 — Bind address
Server binds to `127.0.0.1` only. Outbound calls go only to Ollama at
`http://127.0.0.1:11434`.

#### FR-SV-02 — Endpoints
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/clip` | Main ingestion pipeline |
| `GET` | `/health` | Returns `{"status":"ok"}` |
| `OPTIONS` | `*` | Returns 200 (graceful pre-flight) |

#### FR-SV-03 — Token auth
Requests to `POST /clip` without a matching `X-LocalClip-Token` header receive
`401 Unauthorized`.

#### FR-SV-04 — Body size limit
Reject bodies larger than 2 MB with `413 Payload Too Large`.

#### FR-SV-05 — Closing rule
A turn (user + following assistant) is **closed** when a later user turn exists in the
payload. The **last** turn is **pending** and must not be processed unless `flush: true`.

#### FR-SV-06 — Pipeline for `POST /clip`
Steps in order:

1. **Validate** payload shape and required fields.
2. **Determine closed turns**: from the ordered `messages` list, pair turns; mark the
   last pair as pending unless `flush: true`.
3. **Deduplicate**: load `state.json[conversation_id].last_closed_id`. Skip any turns
   whose user-message `id` is already at or before `last_closed_id`. If all closed turns
   are already processed → `{"status":"duplicate"}`.
4. **For each new closed turn**:
   a. **Save raw**: append to `data/raw/<yyyymmdd-hhmmss>-<slug>.md`.
   b. **LLM call**: `llm.call_ollama(question, answer, existing_topics, recent_bullets,
      config)` → `{skip, title, topic, bullets}`.
   c. **Post-process** via `linker.py`: normalize topic, fuzzy-match, verify links (none
      in v1 LLM schema — see §2.3), remove self-links.
   d. **Append node** to `data/nodes.jsonl`.
   e. **Update** `state.json[conversation_id].last_closed_id` and `count`.
5. **Re-render** vault.
6. Return `{"status":"saved","count":<n>}` (or `"skipped"` / `"duplicate"`).

#### FR-SV-07 — Atomic writes
All writes to `state.json` and `nodes.jsonl` use write-to-temp-then-rename and are
protected by a single `threading.Lock`.

#### FR-SV-08 — Queued mode
If Ollama is unreachable, save raw, mark the entry `status: "queued"` in `state.json`,
return `{"status":"queued"}`. CLI `--reprocess-queued` retries all queued entries.

#### FR-SV-09 — Logging
Log each clip: `[status] topic | N turns | duration_ms ms`. Never log full page text.

---

### 2.3 LLM Integration

#### FR-LLM-01 — Ollama endpoint
`POST http://127.0.0.1:11434/api/chat`, `stream: false`,
`options: {temperature: 0, num_ctx: 8192}`.

#### FR-LLM-02 — JSON schema (per-turn)
One call per closed turn. Input is the question (user markdown) + full answer (assistant
markdown). Output schema:
```json
{
  "type": "object",
  "properties": {
    "skip":    { "type": "boolean" },
    "title":   { "type": "string" },
    "topic":   { "type": "string" },
    "bullets": { "type": "array", "items": {"type":"string"}, "maxItems": 6 }
  },
  "required": ["skip", "title", "topic", "bullets"]
}
```
No `links` field in the LLM schema (v1). Links between nodes are derived structurally
from `prev` pointers; topic links are derived from the `topic` field.

#### FR-LLM-03 — Schema format fallback
If Ollama returns 400/422 for schema-constrained `format`, fall back to `format:"json"`
and validate manually.

#### FR-LLM-04 — System prompt intent
> "You extract durable knowledge from a single question-and-answer exchange. Return only
> facts, decisions, definitions, commands and code snippets worth remembering, copied or
> closely quoted. Ignore greetings, filler, and repeated content. Provide a short
> descriptive title (max 8 words) for this specific exchange. If everything is already
> covered by recent notes, set skip=true. Choose topic from the existing topics list when
> one fits; otherwise propose a short new noun-phrase topic (max 4 words). Never invent
> facts."

#### FR-LLM-05 — User message contents
- Existing topic names (max 80)
- Last 8 bullets from recent nodes (for novelty comparison)
- The question (user turn markdown)
- The answer (assistant turn markdown), truncated to `max_chars` (default 6000)

#### FR-LLM-06 — Retry / failure handling
On invalid JSON: retry once. On second failure: save raw, return
`{"status":"llm_failed"}` without crashing.

#### FR-LLM-07 — Config-driven model
Model name, `num_ctx`, `max_chars` are read from `config.json`.

---

### 2.4 Data Model

#### FR-DM-01 — `data/nodes.jsonl` (single source of truth)
Each line is one JSON object representing one closed turn:
```json
{
  "id":      "<user data-message-id>",
  "conv_id": "<conversation uuid>",
  "idx":     0,
  "prev":    null,
  "question": "...",
  "title":   "...",
  "topic":   "...",
  "bullets": ["..."],
  "raw_file": "yyyymmdd-hhmmss-slug.md",
  "ts":      "<ISO-8601>"
}
```
`prev` is the `id` of the previous node in this conversation (or `null` for the first).
`next` is **never stored** — it is derived at render time by inverting `prev` pointers.

#### FR-DM-02 — `data/state.json`
```json
{
  "conversations": {
    "<conv_id>": {
      "last_closed_id": "<id or null>",
      "count": 3
    }
  },
  "queued": [
    { "raw_file": "...", "url": "...", "title": "...", "ts": "..." }
  ]
}
```

#### FR-DM-03 — Raw archive
`data/raw/<yyyymmdd-hhmmss>-<slug>.md` — one per clip event, never deleted.

#### FR-DM-04 — `config.json`
```json
{
  "port":      8787,
  "token":     "CHANGE_ME",
  "model":     "llama3.2",
  "num_ctx":   8192,
  "max_chars": 6000,
  "blocklist": ["bank","paypal","mail.google.com","outlook","health","medical","login","accounts.google.com"],
  "idle_ms":   5000
}
```

---

### 2.5 Vault Rendering

#### FR-VR-01 — One note per node
Filename: `vault/<conv_short>-<idx> <title>.md`
where `conv_short` is the first 8 characters of the conversation UUID and `title` is the
LLM-generated title (sanitized: remove `\ / : * ? " < > |`).

#### FR-VR-02 — Node note format
```markdown
---
id: <id>
conv_id: <conv_id>
idx: <idx>
topic: <topic>
ts: <ISO-8601>
---

# <title>

**Topic:** [[<topic>]]
Prev: [[<conv_short>-<prev_idx> <prev_title>]] | Next: [[<conv_short>-<next_idx> <next_title>]]

(If no prev: "Prev: (start)" — if no next yet: "Next: (waiting)")
(If branched: "Next: [[A]] [[B]]")

- bullet one
- bullet two

Raw: [[raw/<raw_file>]]
```
Skip Prev/Next links whose target file does not yet exist (dangling prev is tolerated).

#### FR-VR-03 — Topic notes
`vault/<Topic>.md` lists every node for that topic:
```markdown
# <Topic>

## Nodes
- [[<conv_short>-<idx> <title>]] — <YYYY-MM-DD>
- ...
```

#### FR-VR-04 — `vault/_Index.md`
Lists all topics with node count and last-updated date, sorted by most-recent activity.
Also lists all conversations with their node counts.

#### FR-VR-05 — Idempotency
Running `render.py` twice on the same `nodes.jsonl` produces byte-for-byte identical
output. Exposed as `python server/render.py --rebuild`.

#### FR-VR-06 — Next-link derivation
At render time, build an in-memory map `{prev_id → [node_id, ...]}` by scanning all
nodes. A node with two successors (branch) renders both as `Next: [[A]] [[B]]`. A node
with no successor renders `Next: (waiting)`. A dangling `prev` (target missing from
nodes.jsonl) is skipped without error.

---

## 3. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-01 | Zero cloud calls. All computation runs locally. |
| NFR-02 | Python 3.10+. Server uses only stdlib + `requests`. No Flask, no vector DB. |
| NFR-03 | Userscript is plain JavaScript, no build step, runs in Tampermonkey. |
| NFR-04 | The LLM never writes files; it only returns JSON that code writes. |
| NFR-05 | Server binds to `127.0.0.1` only. |
| NFR-06 | Vault must open in Obsidian with a working graph view. |
| NFR-07 | Rendering is deterministic and idempotent. |
| NFR-08 | Concurrent clip requests must not corrupt `nodes.jsonl` or `state.json`. |

---

## 4. Acceptance Criteria

| # | Criterion |
|---|-----------|
| AC-01 | `Alt+S` on a ChatGPT conversation creates node notes and a topic note. |
| AC-02 | Polling auto-sends when the user adds a new turn (count increases). |
| AC-03 | The last (pending) turn is not processed until `flush: true` or a new turn arrives. |
| AC-04 | Clipping the same conversation with no new closed turns returns `duplicate`. |
| AC-05 | A turn marked `skip=true` by LLM creates no node; state advances past it. |
| AC-06 | Prev/Next links in vault correctly chain turns; a branch renders both Next links. |
| AC-07 | A dangling `prev` (target not yet in nodes.jsonl) is skipped, not an error. |
| AC-08 | Server returns 401 for missing or wrong token. |
| AC-09 | `render.py --rebuild` run twice produces identical output. |
| AC-10 | Changing `model` in `config.json` and restarting uses the new model. |
| AC-11 | Raw files are never deleted even on duplicate or skip. |
| AC-12 | The vault graph in Obsidian shows nodes, topic hubs, and Prev/Next edges. |
