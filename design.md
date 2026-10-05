# LocalClip — Design (v1)

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (Tampermonkey — chatgpt.com / chat.openai.com only)   │
│                                                                 │
│  SITE_ADAPTERS["chatgpt.com"]                                   │
│    └─ turnSelector, roleAttr, messageSelector, messageIdAttr   │
│                                                                 │
│  5-second polling loop                                          │
│    read all section[data-testid^="conversation-turn-"]         │
│    count user turns                                             │
│    if count > lastSentCount → POST                             │
│                                                                 │
│  Alt+S ──► POST with flush:true                                │
│  Alt+P ──► toggle pause                                        │
│                                                                 │
│  GM_xmlhttpRequest ──────────────────────────────────────────► │
└─────────────────────────────────────────────────────────────────┘
                         │  POST /clip  (127.0.0.1:8787)
                         │  X-LocalClip-Token: <token>
                         │  {messages:[...], flush:bool, ...}
┌────────────────────────▼────────────────────────────────────────┐
│  server/server.py  (HTTPServer + ThreadingMixIn, 127.0.0.1)    │
│                                                                 │
│  auth → validate → close_turns(flush) → dedupe                 │
│    → for each new closed turn:                                  │
│        store.save_raw()                                         │
│        llm.call_ollama()                                        │
│        linker.process()                                         │
│        store.append_node()                                      │
│        store.update_state()                                     │
│    → render.rebuild()                                           │
│    → {"status":"saved","count":N}                              │
└──────┬──────────────────────────────────────────────────────────┘
       │  POST /api/chat  (127.0.0.1:11434)
┌──────▼────────────┐     ┌───────────────────────────────────────┐
│  Ollama daemon    │     │  Filesystem                           │
│  (local LLM)      │     │                                       │
└───────────────────┘     │  config.json                          │
                          │  data/                                │
                          │    nodes.jsonl  ← single truth        │
                          │    state.json                         │
                          │    raw/<slug>.md                      │
                          │  vault/                               │
                          │    <conv_short>-<idx> <title>.md      │
                          │    <Topic>.md                         │
                          │    _Index.md                          │
                          └───────────────────────────────────────┘
```

---

## 2. Polling vs. MutationObserver — Decision Record

**Removed**: MutationObserver + idle-debounce, stop-button selector, render-state
attribute checks.

**Rationale**: Polling every 5 s is simpler, more robust to DOM restructuring, and
avoids the race condition where a MutationObserver fires before streaming finishes.
The server's closing rule (last turn stays pending) makes polling safe: an in-progress
answer is never processed until the next user message arrives (or `flush:true`).

**Trade-off**: Up to 5 s latency before a completed turn is sent. Acceptable for the
use case (background knowledge capture, not real-time).

---

## 3. Module Breakdown

### 3.1 `server/server.py` — HTTP server + orchestration

| Name | Type | Purpose |
|------|------|---------|
| `LocalClipHandler` | `BaseHTTPRequestHandler` | Routes POST /clip, GET /health, OPTIONS |
| `load_config()` | function | Reads and caches `config.json` |
| `close_turns(messages, flush)` | function | Returns only closed turns; last is pending unless flush |
| `handle_clip(payload, config)` | function | Full pipeline; returns status dict |
| `run_server(config)` | function | Binds HTTPServer + ThreadingMixIn |
| `main()` | function | CLI: `--reprocess-queued` or start server |

**Closing-rule implementation**:
```python
def close_turns(messages: list[dict], flush: bool) -> list[tuple[dict, dict]]:
    """
    Pair messages into (user, assistant) turns in order.
    The last pair is pending unless flush=True.
    Returns list of closed (user, assistant) pairs.
    """
    pairs = []
    i = 0
    while i < len(messages) - 1:
        if messages[i]["role"] == "user" and messages[i+1]["role"] == "assistant":
            pairs.append((messages[i], messages[i+1]))
            i += 2
        else:
            i += 1
    if not flush and pairs:
        pairs = pairs[:-1]   # last pair is pending
    return pairs
```

**Threading**: `ThreadingMixIn` for concurrent requests. A single `threading.Lock`
(owned by `store.py`) guards all file writes.

---

### 3.2 `server/store.py` — State, deduplication, persistence

**Key functions**:

| Name | Signature | Purpose |
|------|-----------|---------|
| `load_state()` | `() → dict` | Read `state.json`; return `{}` if missing |
| `save_state(state)` | `(dict) → None` | Atomic write, under lock |
| `dedupe_turns(conv_id, closed_pairs, state)` | `(str, list, dict) → list` | Return only pairs with user id after last_closed_id |
| `save_raw(payload, turn_pair)` | `(dict, tuple) → str` | Write `data/raw/<slug>.md`; return filename |
| `append_node(node)` | `(dict) → None` | Atomic append to `nodes.jsonl`, under lock |
| `load_nodes()` | `() → list[dict]` | Read all lines from `nodes.jsonl` |
| `update_state(conv_id, new_last_id, state)` | `(...) → None` | Advance last_closed_id + count |
| `slug(text, max_len=40)` | `(str, int) → str` | Filename-safe slug |

**Deduplication logic**:
```python
def dedupe_turns(conv_id, closed_pairs, state):
    conv = state.get("conversations", {}).get(conv_id, {})
    last_id = conv.get("last_closed_id")
    if last_id is None:
        return closed_pairs            # first time: all turns are new
    # find the index of last_id in closed_pairs
    ids = [u["id"] for u, _ in closed_pairs]
    if last_id not in ids:
        return closed_pairs            # last_id not in current batch: all new
    idx = ids.index(last_id)
    return closed_pairs[idx+1:]        # everything after last processed
```

**Node schema**:
```python
node = {
    "id":       user_msg["id"],
    "conv_id":  conv_id,
    "idx":      current_count,         # 0-based, within this conversation
    "prev":     prev_node_id,          # id of previous node, or None
    "question": user_msg["markdown"],
    "title":    llm_result["title"],
    "topic":    llm_result["topic"],
    "bullets":  llm_result["bullets"],
    "raw_file": raw_filename,
    "ts":       datetime.utcnow().isoformat() + "Z",
}
```

**Atomic write**:
```python
def _atomic_write(path: Path, data: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as f:
        f.write(data)
        tmp = f.name
    os.replace(tmp, path)
```

---

### 3.3 `server/llm.py` — Ollama integration

| Name | Signature | Purpose |
|------|-----------|---------|
| `build_schema()` | `() → dict` | JSON schema for per-turn output (no links field) |
| `build_messages(question, answer, existing_topics, recent_bullets)` | `(...) → list[dict]` | System + user messages |
| `_post(payload, config)` | `(dict, dict) → dict` | `requests.post` with 60 s timeout |
| `parse_response(raw)` | `(str) → dict` | Validate skip, title, topic, bullets |
| `call_ollama(question, answer, existing_topics, recent_bullets, config)` | `(...) → dict` | Orchestrate; retry once on bad JSON |

**Schema** (no links in v1):
```python
SCHEMA = {
    "type": "object",
    "properties": {
        "skip":    {"type": "boolean"},
        "title":   {"type": "string"},
        "topic":   {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["skip", "title", "topic", "bullets"],
}
```

**Prompt user message**:
```
## Existing topics
Topic A, Topic B, ...   (max 80, comma-separated)

## Recent bullets (skip if already covered)
- ...  (last 8 from nodes.jsonl)

## Question
<user_markdown>

## Answer
<assistant_markdown, truncated to max_chars>
```

**Version detection**: try `format: SCHEMA` (dict). On HTTP 400/422, set
`_USE_SCHEMA = False`, retry with `format: "json"`.

---

### 3.4 `server/linker.py` — Topic normalization

In v1, `links` are not returned by the LLM. `linker.py` handles only topic
normalization and fuzzy-matching so the topic namespace stays clean.

| Name | Signature | Purpose |
|------|-----------|---------|
| `normalize_topic(name)` | `(str) → str` | Strip, collapse whitespace, Title Case, naive singularize |
| `fuzzy_match(name, existing)` | `(str, list[str]) → str\|None` | difflib ratio ≥ 0.85 → reuse existing |
| `process(llm_result, existing_topics)` | `(dict, list[str]) → dict` | Normalize topic; fuzz-match; return updated result |

**Naive singularization**:
```python
def _singularize(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        return word[:-1]
    return word

def normalize_topic(name: str) -> str:
    name = " ".join(name.split())        # collapse whitespace
    words = name.title().split()
    words[-1] = _singularize(words[-1])  # singularize last word only
    return " ".join(words)
```

---

### 3.5 `server/render.py` — Vault renderer

| Name | Signature | Purpose |
|------|-----------|---------|
| `sanitize_filename(name)` | `(str) → str` | Remove `\ / : * ? " < > \|` |
| `build_next_map(nodes)` | `(list[dict]) → dict[str, list[str]]` | Invert prev pointers: `{prev_id → [node_id, ...]}` |
| `node_filename(node, nodes_by_id)` | `(dict, dict) → str` | `<conv_short>-<idx> <title>` |
| `render_node_file(node, nodes_by_id, next_map)` | `(...) → str` | Full note content |
| `render_topic_file(topic, topic_nodes)` | `(str, list[dict]) → str` | Topic hub note |
| `render_index(nodes, topics)` | `(list, list) → str` | `_Index.md` |
| `rebuild(nodes_path, vault_path)` | `(Path, Path) → None` | Full re-render |
| `main()` | function | CLI: `--rebuild` |

**Next-map construction**:
```python
def build_next_map(nodes: list[dict]) -> dict[str, list[str]]:
    nxt: dict[str, list[str]] = {}
    for node in nodes:
        prev = node.get("prev")
        if prev:
            nxt.setdefault(prev, []).append(node["id"])
    return nxt
```

**Node note template** (deterministic — fields in fixed order):
```
---
id: {id}
conv_id: {conv_id}
idx: {idx}
topic: {topic}
ts: {ts}
---

# {title}

**Topic:** [[{topic}]]
{prev_line} | {next_line}

{bullets}

Raw: [[raw/{raw_file}]]
```

Where:
- `prev_line`: `Prev: (start)` if `prev is None`, else `Prev: [[{prev_filename}]]`
  (omit entirely if prev file doesn't exist yet — dangling prev).
- `next_line`: `Next: (waiting)` if no successors; `Next: [[A]]` if one;
  `Next: [[A]] [[B]]` if branched; skip if target filename not in vault.

**Determinism guarantees**:
- Nodes rendered in `idx` order within each conversation.
- Topic note: nodes sorted by `ts` ascending.
- `_Index.md`: topics sorted by latest node `ts` descending, ties by topic name.
- `build_next_map` processes nodes in `idx` order → branch order is stable.

**Idempotency**: `rebuild` clears the vault directory, then writes all files. Two runs
on the same `nodes.jsonl` produce byte-for-byte identical output because all orderings
are deterministic.

---

### 3.6 `userscript/localclip.user.js`

**Top-level structure**:
```
Constants:    TOKEN, SERVER_URL, IDLE_MS, BLOCKLIST, SITE_ADAPTERS
State:        lastSentUserTurnCount, paused, pollInterval
Functions:
  isBlocked()
  getAdapter()           → adapter or null
  getConversationId()    → string or null
  extractTurns(adapter)  → Promise<{turns, userCount}>
  buildPayload(turns, flush) → object
  sendToServer(payload)  → Promise
  showToast(msg)
  sha256(str)            → Promise<hex>
  normalizeText(str)     → str
  poll()                 → Promise<void>
  init()
```

**SITE_ADAPTERS**:
```js
const SITE_ADAPTERS = {
  "chatgpt.com": {
    enabled: true,
    turnSelector: "section[data-testid^='conversation-turn-']",
    // data-turn attribute value is "user" or "assistant"
    // Verified: DevTools > Elements on live conversation 2025-10
    roleAttr: "turn",
    messageSelector: "[data-message-author-role]",
    messageIdAttr: "data-message-id",
    conversationIdPattern: /\/c\/([0-9a-f-]{36})/,
    stopButtonSelector: null,   // not used in polling model
  },
  // chat.openai.com uses the same adapter
  "chat.openai.com": null,      // populated as alias in init()

  // ── v2 stubs (disabled) ──────────────────────────────────────
  // TODO: verify selectors in DevTools before enabling
  "claude.ai": {
    enabled: false,
    turnSelector: null,
    roleAttr: null,
    messageSelector: null,
    messageIdAttr: null,
    conversationIdPattern: null,
  },
  "gemini.google.com": {
    enabled: false,
    turnSelector: null,
    roleAttr: null,
    messageSelector: null,
    messageIdAttr: null,
    conversationIdPattern: null,
  },
};
```

**Turn extraction** (per adapter):
```js
async function extractTurns(adapter) {
  const sections = [...document.querySelectorAll(adapter.turnSelector)];
  const turns = [];
  let userCount = 0;

  for (const section of sections) {
    const role = section.dataset[adapter.roleAttr];   // "user" | "assistant"
    if (role === "user") userCount++;

    // Collect all message elements within this turn section
    const msgEls = [...section.querySelectorAll(adapter.messageSelector)];
    if (!msgEls.length) continue;

    // ID: data-message-id of first element, else sha256 fallback
    const firstId = msgEls[0].getAttribute(adapter.messageIdAttr);
    const combinedText = msgEls.map(el => {
      const clone = el.cloneNode(true);
      // Strip: button, svg, .sr-only, [role=group], [role=separator]
      clone.querySelectorAll(
        "button, svg, .sr-only, [role='group'], [role='separator']"
      ).forEach(n => n.remove());
      const md = turndown.turndown(clone.innerHTML);
      return md.trim() || clone.innerText.trim();
    }).join("\n\n");

    const id = firstId || await sha256(role + "|" + normalizeText(combinedText));
    turns.push({ id, role, markdown: combinedText });
  }
  return { turns, userCount };
}
```

**Poll loop**:
```js
async function poll() {
  if (paused) return;
  const adapter = getAdapter();
  if (!adapter || !adapter.enabled) return;
  const convId = getConversationId(adapter);
  if (!convId) return;   // new chat, no ID yet

  const { turns, userCount } = await extractTurns(adapter);
  if (userCount <= lastSentUserTurnCount) return;

  const payload = buildPayload(turns, false, convId);
  const result = await sendToServer(payload);
  if (result.status !== "error") lastSentUserTurnCount = userCount;
  showToast(result.status === "saved" ? `saved ${result.count} turn(s)` : result.status);
}

setInterval(poll, 5000);   // started in init()
```

---

## 4. Data Flow Diagrams

### 4.1 Polling — new turn added

```
Browser (every 5s)          Server                Ollama        Filesystem
  │                           │                     │               │
  │ read turns: userCount=3   │                     │               │
  │ lastSent=2 → POST ───────►│                     │               │
  │ {messages:[t1..t6],       │ close_turns(flush=F)│               │
  │  flush:false}             │  → pairs=[(t1,t2),  │               │
  │                           │     (t3,t4)]        │               │
  │                           │  last pair (t5,t6)→pending         │
  │                           │ dedupe: last_closed_id=t3.id        │
  │                           │  → new_pairs=[(t3,t4)]             │
  │                           │ save_raw ─────────────────────────► │
  │                           │ call_ollama(t3,t4) ──────────────► │
  │                           │◄── {title,topic,bullets}            │
  │                           │ linker.process                      │
  │                           │ append_node ──────────────────────► │
  │                           │ update_state ─────────────────────► │
  │                           │ render.rebuild ───────────────────► │
  │◄── {status:"saved",       │                     │               │
  │     count:1}              │                     │               │
  │ lastSent=3                │                     │               │
```

### 4.2 Alt+S flush

```
Browser                     Server
  │                           │
  │──POST {flush:true} ──────►│
  │                           │ close_turns(flush=True)
  │                           │   → ALL pairs including last
  │                           │ process last turn too
  │◄── {status:"saved",...}───│
```

### 4.3 Duplicate

```
Browser                     Server
  │                           │
  │──POST {messages:[t1,t2]} ►│
  │                           │ closed_pairs = [(t1,t2)]
  │                           │ dedupe: last_closed_id = t1.id
  │                           │   → new_pairs = []
  │◄── {status:"duplicate"}───│
```

### 4.4 Branch rendering

```
nodes.jsonl:
  {id:"A", prev:null,  idx:0, ...}
  {id:"B", prev:"A",   idx:1, ...}  ← main branch
  {id:"C", prev:"A",   idx:1, ...}  ← regenerated branch

next_map: {"A": ["B", "C"]}

Vault A.md:  Next: [[conv-1 B title]] [[conv-1 C title]]
Vault B.md:  Prev: [[conv-0 A title]] | Next: (waiting)
Vault C.md:  Prev: [[conv-0 A title]] | Next: (waiting)
```

---

## 5. File Layout

```
localclip/
├── config.json
├── pyproject.toml
├── requirements.md
├── design.md
├── tasks.md
├── README.md
├── server/
│   ├── __init__.py
│   ├── server.py
│   ├── store.py
│   ├── llm.py
│   ├── render.py
│   └── linker.py
├── userscript/
│   └── localclip.user.js
├── data/                    (gitignored)
│   ├── nodes.jsonl
│   ├── state.json
│   └── raw/
├── vault/                   (gitignored)
│   ├── _Index.md
│   ├── <conv_short>-<idx> <title>.md
│   └── <Topic>.md
└── tests/
    ├── test_store.py
    ├── test_linker.py
    ├── test_render.py
    ├── payloads/
    │   ├── chat.json
    │   ├── chat_continued.json
    │   └── duplicate.json
    └── post_payloads.py
```

---

## 6. Key Design Decisions

### 6.1 Polling over MutationObserver
MutationObserver fires continuously during streaming, making idle-debounce timing
fragile and stop-button detection site-dependent. A 5 s poll reads a stable DOM snapshot
and the server closing rule (last turn pending) guarantees in-progress answers are never
processed prematurely.

### 6.2 `nodes.jsonl` — one node per turn, not one per clip
Each turn is an independent knowledge unit. This makes Obsidian graph view useful: nodes
link forward/backward through a conversation, and sideways to topic hubs. Storing `prev`
(not `next`) means the file is append-only; the graph is reconstructed at render time
without rewriting old lines.

### 6.3 No `links` in LLM schema (v1)
Cross-topic links were a source of hallucination in the original spec. In v1, all graph
edges are structural (Prev/Next via `prev` pointer) or categorical (Topic). This is
simpler, correct, and still produces a useful Obsidian graph. Links can be re-added in v2
once the topic namespace is stable.

### 6.4 Branching support
When a user regenerates an assistant response, ChatGPT creates a new message with a
different `data-message-id` but the same logical predecessor. Storing `prev` on each node
and inverting at render time naturally handles branches without special-casing the
ingestion path.

### 6.5 `conv_short` in filenames
Using the first 8 characters of the UUID as a filename prefix groups all nodes from the
same conversation together in the file explorer (they sort together). It's short enough
to be readable and long enough to be practically unique.

### 6.6 Closing rule handles streaming correctly
If the user is still typing / the LLM is still streaming the last answer, the last pair
is always pending. The next poll (or a new user message) closes it. `flush:true` via
`Alt+S` overrides this for manual use.

---

## 7. Security Considerations

- Server binds to `127.0.0.1` only.
- Token is a shared secret; user should set a 32-char random hex string.
- Body size capped at 2 MB.
- Blocklist prevents capture on sensitive pages.
- LLM output is never executed; it is only stored as data.
- No data leaves the machine.
