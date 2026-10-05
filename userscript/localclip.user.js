// ==UserScript==
// @name         LocalClip
// @version      0.3.0
// @description  Capture ChatGPT conversations into a local Obsidian vault via a local server.
// @author       localclip
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @require      https://cdn.jsdelivr.net/npm/turndown@7.2.4/dist/turndown.browser.umd.js
// ==/UserScript==

/* ─────────────────────────────────────────────────────────────────────────────
 * CONFIGURATION
 *
 * TOKEN:          Generate a random string and paste the same value into
 *                 config.json on the server side (the "token" field).
 *                 Example:  openssl rand -hex 16
 *
 * PORT:           Must match the "port" field in config.json.  Default 8787.
 *
 * DEBUG:          Set to false to silence console output once working.
 *
 * ENABLE_HOTKEYS: Alt+S / Alt+P keyboard shortcuts.  Defaulted to false
 *                 because Alt+P opens the browser profile menu on some systems.
 *                 Use the Tampermonkey menu commands instead (always available).
 * ───────────────────────────────────────────────────────────────────────────── */

const TOKEN          = "aa269aac08c3bd71cd3fe289ef3c4808";   // ← paste your token here
const PORT           = 8787;
const DEBUG          = true;    // ← set false to silence [LocalClip] console logs
const ENABLE_HOTKEYS = false;   // ← set true to enable Alt+S / Alt+P

/* ─────────────────────────────────────────────────────────────────────────────
 * INTERNAL CONSTANTS (do not change unless the server moves)
 * ───────────────────────────────────────────────────────────────────────────── */

const SERVER_URL = `http://127.0.0.1:${PORT}`;
const POLL_MS    = 5000;   // polling interval in milliseconds

/* ─────────────────────────────────────────────────────────────────────────────
 * DEBUG LOGGER
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * Log to the console only when DEBUG is true.
 * Always prefixes with "[LocalClip]".
 * Never called with token values or full chat text.
 */
function dbg(...args) {
  if (DEBUG) console.log("[LocalClip]", ...args);
}

/* ─────────────────────────────────────────────────────────────────────────────
 * SITE ADAPTERS
 *
 * Each adapter describes how to read turns from a specific chat site.
 * All selectors for chatgpt.com were verified against a live conversation
 * using DevTools → Elements panel, October 2025.
 *
 * To add a new adapter:
 *   1. Open a live conversation in DevTools.
 *   2. Find the turn container (Elements panel, inspect a message block).
 *   3. Verify each selector with document.querySelectorAll() in the console.
 *   4. Set enabled: true and fill in all fields.
 *   5. Add a @match line and a @connect line to the metadata block.
 * ───────────────────────────────────────────────────────────────────────────── */

const SITE_ADAPTERS = {
  "chatgpt.com": {
    enabled: true,

    // Every conversation turn is a <section> whose data-testid starts with
    // "conversation-turn-".  Verified: DevTools Elements on chatgpt.com, Oct 2025.
    turnSelector: "section[data-testid^='conversation-turn-']",

    // The turn's own data-turn attribute holds "user" or "assistant".
    // Verified: the section element carries data-turn="user" / data-turn="assistant".
    roleAttr: "turn",

    // Message content lives in elements with data-message-author-role.
    // A single turn can contain more than one such element; their text is joined.
    // Verified: DevTools search for [data-message-author-role] inside a turn section.
    messageSelector: "[data-message-author-role]",

    // The first message element's data-message-id is used as the stable ID.
    // Verified: attribute present on the outermost message div.
    messageIdAttr: "data-message-id",

    // Conversation UUID is in the URL path: /c/<uuid>
    // Verified: browser address bar on any named conversation.
    conversationIdPattern: /\/c\/([0-9a-f-]{36})/,

    // Stop button selector — not used in the polling model (kept for reference).
    // If you want to guard against capturing mid-stream, inspect the stop button
    // in DevTools and fill this in.
    stopButtonSelector: null,
  },

  // chat.openai.com is an alias; populated at init() time.
  "chat.openai.com": null,

  // ── v2 stubs — DO NOT enable without verifying selectors in DevTools ────────
  // TODO(claude.ai): open a live conversation in DevTools, find the turn
  // container, verify role attribution, fill in all fields, then set enabled:true.
  "claude.ai": {
    enabled: false,
    turnSelector: null,
    roleAttr: null,
    messageSelector: null,
    messageIdAttr: null,
    conversationIdPattern: null,
    stopButtonSelector: null,
  },

  // TODO(gemini.google.com): same process as claude.ai above.
  "gemini.google.com": {
    enabled: false,
    turnSelector: null,
    roleAttr: null,
    messageSelector: null,
    messageIdAttr: null,
    conversationIdPattern: null,
    stopButtonSelector: null,
  },
};

/* ─────────────────────────────────────────────────────────────────────────────
 * TURNDOWN INSTANCE
 * ───────────────────────────────────────────────────────────────────────────── */

// TurndownService is injected by @require above.
/* global TurndownService */
const _td = new TurndownService({
  headingStyle:   "atx",
  codeBlockStyle: "fenced",
  bulletListMarker: "-",
});

// Preserve fenced code blocks with language hints.
_td.addRule("fencedCodeBlock", {
  filter: (node) =>
    node.nodeName === "PRE" &&
    node.firstChild &&
    node.firstChild.nodeName === "CODE",
  replacement: (_content, node) => {
    const code = node.firstChild;
    const lang = (code.getAttribute("class") || "")
      .replace(/^language-/, "")
      .trim();
    const text = code.textContent || "";
    return `\n\n\`\`\`${lang}\n${text.replace(/\n$/, "")}\n\`\`\`\n\n`;
  },
});

/* ─────────────────────────────────────────────────────────────────────────────
 * MODULE STATE
 * ───────────────────────────────────────────────────────────────────────────── */

// Per-conversation acknowledged user-turn count.
// Reset on each page load; the server dedupes by message id.
const _ackedCount = {};   // { [conv_id]: number }

let _paused       = false;
let _lastToast    = "";   // suppress identical consecutive toasts
let _pollTimer    = null;

/* ─────────────────────────────────────────────────────────────────────────────
 * PURE HELPERS  (exported via _LC for Node test access)
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * Return the conversation UUID from a pathname, or null if absent.
 * Pattern: /c/<uuid-36-chars>
 *
 * @param {string} pathname  e.g. "/c/aaaabbbb-0000-0000-0000-000000000001"
 * @returns {string|null}
 */
function getConversationId(pathname) {
  const pattern = /\/c\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/;
  const m = pathname.match(pattern);
  return m ? m[1] : null;
}

/**
 * Collapse runs of whitespace (including newlines) to single spaces and trim.
 *
 * @param {string} str
 * @returns {string}
 */
function normalizeText(str) {
  return str.replace(/\s+/g, " ").trim();
}

/**
 * SHA-256 of a string via SubtleCrypto.  Returns a hex string.
 * Async because SubtleCrypto.digest() is Promise-based.
 *
 * @param {string} str
 * @returns {Promise<string>}
 */
async function sha256hex(str) {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(str)
  );
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Derive a stable message id.
 *   - Primary:  data-message-id attribute of the first message element in the turn.
 *   - Fallback: sha256(role + "|" + normalizeText(markdown))
 *
 * @param {string|null} attrId   value of data-message-id (may be null/empty)
 * @param {string}      role     "user" | "assistant"
 * @param {string}      markdown normalized text content
 * @returns {Promise<string>}
 */
async function deriveMessageId(attrId, role, markdown) {
  if (attrId && attrId.trim()) return attrId.trim();
  return sha256hex(role + "|" + normalizeText(markdown));
}

/**
 * Given an ordered list of {id, role, markdown} messages, return a list of
 * {userCount, messages} — just the messages list plus the count of user-role
 * entries.  Pure: does not touch the DOM.
 *
 * @param {Array<{id:string, role:string, markdown:string}>} messages
 * @returns {{ messages: Array, userCount: number }}
 */
function summarizeTurns(messages) {
  const userCount = messages.filter((m) => m.role === "user").length;
  return { messages, userCount };
}

/* ─────────────────────────────────────────────────────────────────────────────
 * DOM EXTRACTION
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * Strip elements that should not appear in the markdown output.
 * Operates on a *clone* — never touches the live page.
 *
 * @param {Element} clone
 */
function stripNoise(clone) {
  const selectors = [
    "button",
    "svg",
    ".sr-only",
    "[role='group']",
    "[role='separator']",
  ];
  clone.querySelectorAll(selectors.join(", ")).forEach((el) => el.remove());
}

/**
 * Convert a single message element to markdown.
 * Clones the element, strips noise, runs Turndown.
 * Falls back to innerText if Turndown returns empty.
 *
 * @param {Element} msgEl
 * @returns {string}
 */
function messageToMarkdown(msgEl) {
  const clone = msgEl.cloneNode(true);
  stripNoise(clone);
  const md = _td.turndown(clone.innerHTML || "").trim();
  return md || (clone.textContent || "").trim();
}

/**
 * Extract all turns from the page using the given adapter.
 * Returns an array of {id, role, markdown} objects in DOM order.
 *
 * @param {object} adapter   a SITE_ADAPTERS entry
 * @returns {Promise<Array<{id:string, role:string, markdown:string}>>}
 */
async function extractTurns(adapter) {
  const sections = Array.from(
    document.querySelectorAll(adapter.turnSelector)
  );
  const turns = [];

  for (const section of sections) {
    // Role comes from data-<roleAttr> on the section element itself.
    const role = section.dataset[adapter.roleAttr];
    if (role !== "user" && role !== "assistant") continue;

    // Collect all message elements within this turn.
    const msgEls = Array.from(section.querySelectorAll(adapter.messageSelector));
    if (msgEls.length === 0) continue;

    // Build markdown by converting each message element and joining.
    const parts = msgEls.map(messageToMarkdown).filter(Boolean);
    const markdown = parts.join("\n\n");

    // ID: attribute of the first element, or hash fallback.
    const attrId = msgEls[0].getAttribute(adapter.messageIdAttr);
    const id = await deriveMessageId(attrId, role, markdown);

    turns.push({ id, role, markdown });
  }

  return turns;
}

/* ─────────────────────────────────────────────────────────────────────────────
 * TOAST
 * ───────────────────────────────────────────────────────────────────────────── */

let _toastEl = null;
let _toastTimer = null;

/**
 * Show a small toast in the bottom-right corner.
 * Suppresses identical consecutive messages to avoid spam.
 *
 * @param {string} msg
 */
function showToast(msg) {
  if (msg === _lastToast) return;
  _lastToast = msg;

  if (!_toastEl) {
    _toastEl = document.createElement("div");
    Object.assign(_toastEl.style, {
      position:     "fixed",
      bottom:       "20px",
      right:        "20px",
      zIndex:       "999999",
      background:   "#1a1a2e",
      color:        "#e0e0e0",
      padding:      "8px 14px",
      borderRadius: "6px",
      fontSize:     "13px",
      fontFamily:   "monospace",
      boxShadow:    "0 2px 8px rgba(0,0,0,0.4)",
      pointerEvents:"none",
      transition:   "opacity 0.2s",
    });
    document.body.appendChild(_toastEl);
  }

  _toastEl.textContent = `LocalClip: ${msg}`;
  _toastEl.style.opacity = "1";

  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    if (_toastEl) _toastEl.style.opacity = "0";
    // Reset _lastToast after the toast fades so the next identical result
    // still shows if it comes after a gap.
    setTimeout(() => { _lastToast = ""; }, 300);
  }, 3000);
}

/* ─────────────────────────────────────────────────────────────────────────────
 * SERVER COMMUNICATION
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * POST a clip payload to the local server.
 * Returns a Promise that resolves to the parsed JSON response,
 * or rejects with a plain object {status, message} on any error.
 *
 * @param {object} payload
 * @returns {Promise<object>}
 */
function postClip(payload) {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method:  "POST",
      url:     `${SERVER_URL}/clip`,
      headers: {
        "Content-Type":        "application/json",
        "X-LocalClip-Token":   TOKEN,
      },
      data: JSON.stringify(payload),

      onload(resp) {
        let body;
        try {
          body = JSON.parse(resp.responseText);
        } catch (_) {
          body = { status: "error", message: "invalid JSON from server" };
        }

        dbg(`response HTTP ${resp.status}`, body);

        if (resp.status === 401) {
          reject({ status: "error", message: "wrong token (401)" });
          return;
        }
        if (resp.status === 413) {
          reject({ status: "error", message: "payload too large (413)" });
          return;
        }
        if (resp.status >= 400) {
          reject({ status: "error", message: `server error (${resp.status})` });
          return;
        }
        resolve(body);
      },

      onerror() {
        dbg("request error — server unreachable");
        reject({ status: "error", message: "server unreachable — is it running?" });
      },

      ontimeout() {
        dbg("request timed out");
        reject({ status: "error", message: "request timed out" });
      },

      timeout: 15000,
    });
  });
}

/* ─────────────────────────────────────────────────────────────────────────────
 * ADAPTER LOOKUP
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * Return the enabled adapter for the current hostname, or null.
 *
 * @returns {object|null}
 */
function getAdapter() {
  const host = location.hostname;
  const adapter = SITE_ADAPTERS[host];
  if (!adapter || !adapter.enabled) return null;
  return adapter;
}

/* ─────────────────────────────────────────────────────────────────────────────
 * CLIP (core action)
 * ───────────────────────────────────────────────────────────────────────────── */

/**
 * Extract turns, check whether they are new, and POST to the server.
 *
 * @param {boolean} [flush=false]  true = process last (pending) turn too
 */
async function clip(flush = false) {
  const adapter = getAdapter();
  if (!adapter) return;

  const convId = getConversationId(location.pathname);
  if (!convId) {
    dbg("poll decision: no conversation id");
    // New chat: URL has no conversation ID yet.
    if (flush) showToast("no conversation ID yet — save after ChatGPT responds");
    return;
  }

  if (_paused) {
    dbg("poll decision: paused");
    return;
  }

  let turns;
  try {
    turns = await extractTurns(adapter);
  } catch (err) {
    showToast(`extraction error: ${err && err.message ? err.message : err}`);
    return;
  }

  const { userCount } = summarizeTurns(turns);
  const ackedCount = _ackedCount[convId] || 0;

  dbg(
    `poll — turns found: ${turns.length}, user turns: ${userCount},` +
    ` acked: ${ackedCount}, flush: ${flush}`
  );

  // For the polling path: only POST when we have new user turns.
  // For flush (Alt+S): always POST regardless of count.
  if (!flush && userCount <= ackedCount) {
    dbg("poll decision: skip (no new user turns)");
    return;
  }

  dbg("poll decision: send");

  const payload = {
    url:             location.href,
    title:           document.title,
    source:          "chat",
    conversation_id: convId,
    messages:        turns,
    flush:           flush,
    captured_at:     new Date().toISOString(),
  };

  let result;
  try {
    result = await postClip(payload);
  } catch (err) {
    showToast(err.message || "error");
    return;
  }

  // Update acknowledged count on any non-error response.
  _ackedCount[convId] = userCount;

  const s = result.status;
  if (s === "saved")      showToast(`saved ${result.count} turn(s)`);
  else if (s === "duplicate") showToast("nothing new");
  else if (s === "skipped")   showToast("nothing new");
  else if (s === "queued")    showToast("queued (Ollama offline)");
  else if (s === "llm_failed") showToast("saved (LLM error — raw only)");
  else                         showToast(s || "ok");
}

/* ─────────────────────────────────────────────────────────────────────────────
 * POLL LOOP
 * ───────────────────────────────────────────────────────────────────────────── */

async function poll() {
  try {
    await clip(false);
  } catch (_) {
    // clip() handles its own errors; belt-and-suspenders catch
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
 * HOTKEYS
 * ───────────────────────────────────────────────────────────────────────────── */

function onKeyDown(e) {
  // Alt+S — manual flush
  if (e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey && e.key === "s") {
    e.preventDefault();
    clip(true);
    return;
  }
  // Alt+P — toggle pause
  if (e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey && e.key === "p") {
    e.preventDefault();
    _paused = !_paused;
    showToast(_paused ? "auto-capture paused" : "auto-capture resumed");
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
 * INIT
 * ───────────────────────────────────────────────────────────────────────────── */

function init() {
  // Wire chat.openai.com to the same adapter as chatgpt.com.
  SITE_ADAPTERS["chat.openai.com"] = SITE_ADAPTERS["chatgpt.com"];

  // Only proceed if this is a supported, enabled site.
  if (!getAdapter()) return;

  // ── Load-time debug log ──────────────────────────────────────────────────
  const _version      = "0.3.0";
  const _hostname     = location.hostname;
  const _convId       = getConversationId(location.pathname) || "none";
  const _tokenWarning = (TOKEN === "CHANGE_ME") ? " ⚠ TOKEN is still placeholder!" : "";
  dbg(`v${_version} | host: ${_hostname} | conv: ${_convId}${_tokenWarning}`);
  // ────────────────────────────────────────────────────────────────────────

  // Hotkeys (off by default — Alt+P opens browser profile on some systems).
  // Enable by setting ENABLE_HOTKEYS = true at the top of this file.
  if (ENABLE_HOTKEYS) {
    document.addEventListener("keydown", onKeyDown, true);
  }

  // Tampermonkey menu commands (always available regardless of ENABLE_HOTKEYS).
  /* global GM_registerMenuCommand */
  GM_registerMenuCommand("Save now (flush)", () => clip(true));
  GM_registerMenuCommand("Pause/resume auto-capture", () => {
    _paused = !_paused;
    showToast(_paused ? "auto-capture paused" : "auto-capture resumed");
  });

  _pollTimer = setInterval(poll, POLL_MS);

  // Show a brief "active" toast so the user can confirm the script loaded
  // without opening the console.  Uses a separate path so it bypasses the
  // duplicate-suppression logic in showToast() and dismisses after 2 s.
  (() => {
    const el = document.createElement("div");
    Object.assign(el.style, {
      position:     "fixed",
      bottom:       "20px",
      right:        "20px",
      zIndex:       "999999",
      background:   "#1a1a2e",
      color:        "#e0e0e0",
      padding:      "8px 14px",
      borderRadius: "6px",
      fontSize:     "13px",
      fontFamily:   "monospace",
      boxShadow:    "0 2px 8px rgba(0,0,0,0.4)",
      pointerEvents:"none",
      transition:   "opacity 0.3s",
      opacity:      "1",
    });
    el.textContent = "LocalClip active";
    document.body.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 400);
    }, 2000);
  })();
}

init();

/* ─────────────────────────────────────────────────────────────────────────────
 * EXPORTS FOR TESTING (Node.js only — never reached in browser)
 * ───────────────────────────────────────────────────────────────────────────── */

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    getConversationId,
    normalizeText,
    sha256hex,
    deriveMessageId,
    summarizeTurns,
  };
}
