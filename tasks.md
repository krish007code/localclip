# LocalClip — Tasks (v1)

Build order: each checkpoint is atomic, tested, and committed before the next starts.

---

## Checkpoint 1 — Store + Render + Stub LLM

Goal: the full data pipeline works end-to-end with a stub LLM. `nodes.jsonl` is
populated, the vault is rendered correctly including Prev/Next links and topic notes,
and the server accepts requests. No real Ollama call yet.

### T-01 · Project scaffolding
- [ ] Create `config.json` with default values (port, token placeholder, model, num_ctx,
      max_chars, blocklist, idle_ms).
- [ ] Create `data/`, `vault/`, `data/raw/` directories (add `.gitkeep` so they commit).
- [ ] Add `data/` and `vault/` to `.gitignore` (keep `.gitkeep` files).
- [ ] Update `pyproject.toml`: correct `requires-python = ">=3.10"` (currently says
      3.14).
- [ ] Create `server/__init__.py`.

### T-02 · `server/store.py`
- [ ] `slug(text, max_len=40) → str`: lowercase, replace non-alphanumeric with `-`,
      strip leading/trailing `-`.
- [ ] `load_state() → dict`: read `data/state.json`; return `{}` if missing.
- [ ] `save_state(state) → None`: atomic write (temp+rename), under `_LOCK`.
- [ ] `dedupe_turns(conv_id, closed_pairs, state) → list`: return only pairs whose
      user-message id comes after `state["conversations"][conv_id]["last_closed_id"]`.
- [ ] `save_raw(payload, user_turn, asst_turn) → str`: write
      `data/raw/<yyyymmdd-hhmmss>-<slug>.md` with YAML header (url, title, date,
      question, answer); return basename only.
- [ ] `append_node(node) → None`: atomic append to `data/nodes.jsonl`, under `_LOCK`.
- [ ] `load_nodes() → list[dict]`: read all lines; skip and warn on malformed lines.
- [ ] `update_state(conv_id, new_last_id, state) → None`: advance `last_closed_id` and
      increment `count`; save state.

### T-03 · `server/linker.py`
- [ ] `normalize_topic(name) → str`: strip, collapse whitespace, Title Case, naive
      singularize (last word only: `ies→y`, strip trailing `s` if len>4 and not `ss`).
- [ ] `fuzzy_match(name, existing) → str | None`: difflib `SequenceMatcher` ratio ≥ 0.85.
- [ ] `process(llm_result, existing_topics) → dict`: normalize topic; if fuzzy match
      found, replace with existing name; return updated result.

### T-04 · `server/render.py`
- [ ] `sanitize_filename(name) → str`: remove `\ / : * ? " < > |`.
- [ ] `build_next_map(nodes) → dict[str, list[str]]`: invert `prev` pointers;
      process nodes in `idx` order for stable branch ordering.
- [ ] `node_filename(node) → str`: `<conv_short>-<idx> <sanitized_title>`.
- [ ] `render_node_file(node, nodes_by_id, next_map, all_filenames) → str`:
      YAML frontmatter + title + Topic link + Prev/Next lines + bullets + Raw link.
      Skip Prev/Next link if target filename not in `all_filenames` (dangling tolerance).
      Branch: `Next: [[A]] [[B]]`. No next: `Next: (waiting)`.
- [ ] `render_topic_file(topic, topic_nodes, all_filenames) → str`: topic hub note
      listing node wiki-links sorted by `ts` ascending.
- [ ] `render_index(nodes) → str`: `_Index.md` with topics (node count, last ts) sorted
      by latest `ts` desc, and conversations (count, last ts) sorted likewise.
- [ ] `rebuild(nodes_path, vault_path) → None`: clear vault, re-render all node files,
      all topic files, `_Index.md`.
- [ ] `main()`: parse `--rebuild` flag; call `rebuild()`.

### T-05 · `server/server.py` (stub LLM)
- [ ] `load_config() → dict`: read + cache `config.json`.
- [ ] `close_turns(messages, flush) → list[tuple]`: pair user+assistant in order; drop
      last pair unless `flush=True`.
- [ ] `LocalClipHandler`: route `GET /health`, `OPTIONS *`, `POST /clip`.
- [ ] Auth: reject 401 if `X-LocalClip-Token` doesn't match config.
- [ ] Size: reject 413 if `Content-Length > 2097152` (2 MB).
- [ ] `handle_clip(payload, config) → dict`: full pipeline using stub LLM:
      `stub_llm(q, a) → {skip:False, title:"Stub Turn", topic:"Stub Topic", bullets:["stub"]}`.
- [ ] `run_server(config)`: `ThreadingMixIn` + `HTTPServer` on `127.0.0.1:port`.
- [ ] `main()`: `--reprocess-queued` flag placeholder (wired in checkpoint 4); otherwise
      start server.

### T-06 · Unit tests — Checkpoint 1
- [ ] `tests/test_store.py`:
  - `slug()`: spaces, special chars, max_len truncation.
  - `dedupe_turns()`: empty state (all turns new), partial state (some turns new), all
    turns already seen (empty result), conv_id not in state (all new).
  - `update_state()`: last_closed_id advances, count increments.
  - Atomic write: writing to `nodes.jsonl` twice appends correctly.
- [ ] `tests/test_linker.py`:
  - `normalize_topic()`: whitespace collapse, Title Case, plural `ies→y`, plain `s`
    stripped, `ss` not stripped, short word not stripped.
  - `fuzzy_match()`: exact match → returns existing; close match (ratio ≥ 0.85) → reuse;
    distant names → None.
  - `process()`: topic gets normalized + fuzz-matched.
- [ ] `tests/test_render.py`:
  - `sanitize_filename()`: all forbidden chars removed.
  - `build_next_map()`: linear chain, branch (two nodes same prev), root node.
  - `render_node_file()`: first node (`Prev: (start)`, `Next: (waiting)`); middle node
    with valid prev/next; branch node with two nexts; dangling prev (target missing →
    prev line omitted).
  - `rebuild()` idempotency: run twice on same `nodes.jsonl`, compare all files
    byte-for-byte.

### T-07 · Checkpoint 1 commit
- [ ] Smoke-test: start server, POST a sample payload (two turns, flush=false → one
      processed; flush=true → both processed). Confirm `vault/` populated, `_Index.md`
      exists, Prev/Next links correct.
- [ ] `git add` specific files only (no `data/`, `vault/` contents).
- [ ] `git commit -m "checkpoint-1: store, render, server stub"`.

---

## Checkpoint 2 — Userscript

Goal: the Tampermonkey script polls ChatGPT, extracts turns correctly, POSTs to server,
and shows toasts. Claude/Gemini are present as disabled stubs.

### T-08 · Metadata block
- [ ] `@name`, `@version 0.1.0`, `@description`.
- [ ] `@match https://chatgpt.com/*` and `@match https://chat.openai.com/*`.
- [ ] `@grant GM_xmlhttpRequest`.
- [ ] `@connect 127.0.0.1` and `@connect localhost`.
- [ ] `@require` Readability `0.6.0` and Turndown `7.2.4` (pinned jsdelivr URLs).

### T-09 · Constants and utilities
- [ ] `TOKEN`, `SERVER_URL`, `IDLE_MS` (5000), `BLOCKLIST` constants at top.
- [ ] `isBlocked() → bool`: check `location.hostname` against each entry as substring.
- [ ] `normalizeText(str) → str`: trim, collapse internal whitespace.
- [ ] `sha256(str) → Promise<hex>`: SubtleCrypto `SHA-256`.
- [ ] `showToast(msg)`: fixed bottom-right overlay, z-index 999999, auto-dismiss 3 s,
      inlined CSS only (no classes that could conflict with site styles).

### T-10 · `SITE_ADAPTERS` object
- [ ] `chatgpt.com` entry: all verified fields with DevTools verification comment.
- [ ] `chat.openai.com` aliased to same adapter in `init()`.
- [ ] `claude.ai` stub: `enabled: false`, all selectors `null`, TODO comment.
- [ ] `gemini.google.com` stub: `enabled: false`, all selectors `null`, TODO comment.

### T-11 · Extraction functions
- [ ] `getAdapter() → adapter | null`: match `location.hostname` against SITE_ADAPTERS;
      return null if `enabled: false` or not found.
- [ ] `getConversationId(adapter) → string | null`: test `location.pathname` against
      `adapter.conversationIdPattern`; return null if no match.
- [ ] `extractTurns(adapter) → Promise<{turns, userCount}>`:
  - Query `adapter.turnSelector`.
  - For each section: read role from `dataset[adapter.roleAttr]`.
  - Collect all `adapter.messageSelector` descendants; join their cleaned markdown.
  - Clone each message element; strip `button, svg, .sr-only, [role="group"],
    [role="separator"]`; Turndown; fallback to `innerText` if empty.
  - ID: `getAttribute(adapter.messageIdAttr)` on first element, else sha256 fallback.
  - Count user-role sections.
- [ ] `buildPayload(turns, flush, convId) → object`: assemble per FR-US-11.
- [ ] `sendToServer(payload) → Promise<object>`: `GM_xmlhttpRequest` POST; resolve with
      parsed JSON; reject with `{status:"error", message:"..."}` on failure.

### T-12 · Polling and hotkeys
- [ ] Module-level state: `lastSentUserTurnCount = 0`, `paused = false`.
- [ ] `poll() → Promise<void>`: full cycle (guard → extract → compare count → POST →
      update count → toast).
- [ ] `setInterval(poll, 5000)` started in `init()`.
- [ ] `Alt+S` keydown listener: extract → POST with `flush: true` → toast.
- [ ] `Alt+P` keydown listener: toggle `paused`, show toast with new state.

### T-13 · `init()`
- [ ] Check `isBlocked()` → return early if blocked.
- [ ] Wire `chat.openai.com` alias.
- [ ] Register hotkey listeners.
- [ ] Start poll interval.
- [ ] Guard: if `getAdapter()` returns null (not a supported or enabled site), exit.

### T-14 · Checkpoint 2 commit
- [ ] Manual test in Tampermonkey: open a ChatGPT conversation, wait 5 s, confirm toast
      and server log. Add a message, confirm new turn is picked up on next poll.
      Test `Alt+S` flush. Test `Alt+P` pause/resume.
- [ ] `git commit -m "checkpoint-2: userscript polling and extraction"`.

---

## Checkpoint 3 — Real Ollama call with JSON schema

Goal: the server calls a real local LLM, validates output, and writes real nodes.

### T-15 · `server/llm.py`
- [ ] `build_schema() → dict`: JSON schema per FR-LLM-02 (skip, title, topic, bullets).
- [ ] `build_messages(question, answer, existing_topics, recent_bullets) → list[dict]`:
      system prompt per FR-LLM-04; user message per FR-LLM-05; truncate answer to
      `max_chars`.
- [ ] `_post(payload, config) → dict`: `requests.post` with 60 s timeout; raise
      `ConnectionError` if unreachable.
- [ ] `parse_response(raw_json) → dict`: check all required fields; raise `ValueError` on
      bad shape.
- [ ] `call_ollama(question, answer, existing_topics, recent_bullets, config) → dict`:
      orchestrate; retry once on `ValueError`; return `{"skip":True, "_error":"llm_failed"}`
      on second failure.
- [ ] Schema-version detection: try `format: dict`; on HTTP 400/422 set `_USE_SCHEMA = False`
      and retry with `format: "json"`.

### T-16 · Wire LLM into `server.py`
- [ ] Replace stub LLM in `handle_clip` with `llm.call_ollama`.
- [ ] Handle `ConnectionError` → save raw, mark queued, return `{"status":"queued"}`.
- [ ] Handle `_error: "llm_failed"` → save raw only, return `{"status":"llm_failed"}`.
- [ ] Handle `skip: True` → advance `last_closed_id` (mark processed), no node appended,
      return `{"status":"skipped"}`.
- [ ] Log: `[status] topic | N turns | Xms`.

### T-17 · Checkpoint 3 commit
- [ ] End-to-end test: clip a real ChatGPT conversation (requires Ollama running with
      model from config). Confirm `vault/` has a real topic and real bullets.
- [ ] `git commit -m "checkpoint-3: real Ollama LLM integration"`.

---

## Checkpoint 4 — Dedupe, closing logic, queued mode

Goal: repeated clips return `duplicate`; only new turns are processed; `Alt+S` flush
closes the last turn; `--reprocess-queued` works.

### T-18 · Closing logic verification
- [ ] Test `close_turns()`: 3 pairs → flush=False returns 2; flush=True returns 3.
- [ ] Test `close_turns()`: 1 pair → flush=False returns 0; flush=True returns 1.
- [ ] Test `close_turns()`: odd number of messages (trailing user with no assistant) →
      incomplete pair is silently dropped.

### T-19 · Dedupe verification
- [ ] End-to-end: clip conversation → `saved`. Clip again (same turns) → `duplicate`.
- [ ] End-to-end: add 2 more turns → `saved 1` (only new closed turn processed).
- [ ] Unit test in `test_store.py`: `dedupe_turns` when `last_closed_id` not in current
      batch (e.g. earlier conversation replay) → treat all as new.

### T-20 · `--reprocess-queued` CLI
- [ ] `main()` in `server.py`: read `state["queued"]`, re-run LLM + render per entry;
      remove from queue on success; leave on failure.
- [ ] Manual test: kill Ollama → clip → `queued`. Start Ollama → run
      `python server/server.py --reprocess-queued` → entry processed.

### T-21 · Sample payloads
- [ ] `tests/payloads/chat.json`: 4 turns (2 closed + 1 pending), flush=false.
- [ ] `tests/payloads/chat_continued.json`: 6 turns (same conv_id as chat.json, 2
      additional closed turns).
- [ ] `tests/payloads/duplicate.json`: identical to `chat.json`.
- [ ] `tests/post_payloads.py`: POST all three in sequence; print results; assert
      `saved` → `duplicate` → `saved` pattern.

### T-22 · Checkpoint 4 commit
- [ ] Run `tests/post_payloads.py` against live server; confirm output.
- [ ] `git commit -m "checkpoint-4: dedupe, closing rule, queued mode"`.

---

## Checkpoint 5 — Linker and graph rendering

Goal: topic normalization, fuzzy-matching, and Prev/Next graph are correct in the vault.

### T-23 · Wire linker into pipeline
- [ ] In `handle_clip`, after `llm.call_ollama`, call `linker.process(llm_result,
      existing_topics)` (where `existing_topics` comes from `load_nodes()`).
- [ ] Confirm fuzzy-matched topics reuse existing name in the node file.

### T-24 · Branch rendering
- [ ] Manually craft a `nodes.jsonl` with a branch (two nodes sharing the same `prev`).
- [ ] Run `render.py --rebuild`. Confirm parent node has `Next: [[A]] [[B]]`.
- [ ] Confirm each child has correct `Prev` link.

### T-25 · Dangling prev tolerance
- [ ] Craft a `nodes.jsonl` where a node's `prev` ID doesn't exist.
- [ ] Run `render.py --rebuild`. Confirm no crash; prev line omitted for that node.

### T-26 · Unit tests — linker + render (branch, dangling)
- [ ] `test_linker.py`: `process()` with a topic that fuzzy-matches an existing one
      (ratio 0.87) → returns existing name.
- [ ] `test_render.py`: branch case (see design §4.4) — parent has two Next links; each
      child has one Prev link.
- [ ] `test_render.py`: dangling prev — node with non-existent `prev` renders without
      Prev line.
- [ ] `test_render.py`: `rebuild()` idempotency with branch in `nodes.jsonl`.

### T-27 · Checkpoint 5 commit
- [ ] `git commit -m "checkpoint-5: linker, topic normalization, graph rendering"`.

---

## Checkpoint 6 — Security and edge-case hardening

Goal: all guards are tested; edge cases are handled gracefully.

### T-28 · Token enforcement tests
- [ ] POST without token → 401.
- [ ] POST with wrong token → 401.
- [ ] POST with correct token → 200.

### T-29 · Body size test
- [ ] POST with body > 2 MB → 413.

### T-30 · Blocklist tests (userscript)
- [ ] `isBlocked()` returns true for each default entry.
- [ ] `isBlocked()` returns false for `chatgpt.com`.

### T-31 · New-chat gating test
- [ ] `getConversationId()` returns null when pathname is `/` or `/new`.
- [ ] Poll loop does not POST when `convId` is null.

### T-32 · Concurrent write safety
- [ ] Spawn two threads both calling `append_node` simultaneously; confirm
      `nodes.jsonl` has exactly two valid lines afterwards.

### T-33 · Checkpoint 6 commit
- [ ] `git commit -m "checkpoint-6: security hardening and edge cases"`.

---

## Checkpoint 7 — README and setup guide

Goal: a non-technical friend can set up LocalClip in under 5 minutes.

### T-34 · README sections
- [ ] **What is LocalClip**: one paragraph, plain language.
- [ ] **Why open source / local**: privacy, zero cost, offline use, swapping models.
- [ ] **Requirements**: Python 3.10+, Ollama, Tampermonkey, Chrome/Firefox/Edge.
- [ ] **5-minute setup guide**: numbered steps — clone, install Ollama + pull model,
      edit `config.json` (set token to a random string), `python server/server.py`,
      install userscript in Tampermonkey, paste token constant, open ChatGPT and test
      with `Alt+S`.
- [ ] **Keyboard shortcuts** table.
- [ ] **Changing the model**: one-line change in `config.json`.
- [ ] **Reprocessing queued clips**: `--reprocess-queued` command.
- [ ] **Opening in Obsidian**: point vault folder at `localclip/vault/`.
- [ ] **Troubleshooting**: server not running, Ollama offline, wrong token, ChatGPT DOM
      changed (how to find new selector in DevTools).

### T-35 · Checkpoint 7 commit
- [ ] `git commit -m "checkpoint-7: README and setup guide"`.

---

## Full Acceptance Test Run

| # | Test | Expected |
|---|------|----------|
| AC-01 | Alt+S on ChatGPT | Node note + topic note + `_Index.md` entry created |
| AC-02 | Poll picks up new turn | `saved 1 turn(s)` toast after poll interval |
| AC-03 | Last turn pending | Last turn not processed until new turn arrives or flush |
| AC-04 | Same clip again | `duplicate` |
| AC-05 | LLM `skip=true` | No node appended; `last_closed_id` still advances |
| AC-06 | Prev/Next chain | Correct links in vault; branch shows two Next |
| AC-07 | Dangling prev | No crash; prev line omitted |
| AC-08 | Wrong token | 401 |
| AC-09 | `render.py --rebuild` twice | Byte-for-byte identical output |
| AC-10 | Change model in config | New model used after restart |
| AC-11 | Raw files | Never deleted; exist even for duplicates/skips |
| AC-12 | Obsidian graph | Nodes, topic hubs, and Prev/Next edges visible |

---

## Constraints Reminder

- No new third-party Python library without approval (stdlib + `requests` only).
- No hashed or Tailwind class names in the userscript.
- Claude and Gemini adapters remain as disabled stubs; do not enable or fill in selectors
  without verified DevTools inspection.
- The LLM never writes files. It returns JSON only.
- Each checkpoint ends with a `git commit` before the next begins.
