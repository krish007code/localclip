/**
 * tests/test_userscript.mjs
 *
 * Node-runnable pure-function tests for the LocalClip userscript helpers.
 *
 * Run with:
 *   node tests/test_userscript.mjs
 *
 * No test framework needed — uses Node's built-in assert module.
 * All tested functions are pure (no DOM, no GM_*, no browser globals).
 *
 * The userscript file uses `module.exports` when running under Node
 * (the `typeof module !== "undefined"` guard at the bottom of the file).
 * TurndownService is referenced in the top-level scope of the userscript,
 * so we must stub it before requiring the file.
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

// ── Stubs required before the userscript is loaded ────────────────────────

// TurndownService is @require'd by Tampermonkey; provide a minimal stub
// so the top-level `new TurndownService(...)` call doesn't throw.
globalThis.TurndownService = class {
  constructor() {}
  addRule() {}
  turndown() { return ""; }
};

// GM_xmlhttpRequest is injected by Tampermonkey; stub it so init() doesn't crash.
globalThis.GM_xmlhttpRequest = () => {};

// GM_registerMenuCommand is injected by Tampermonkey; stub it.
globalThis.GM_registerMenuCommand = () => {};

// GM_getValue / GM_setValue are injected by Tampermonkey; stub with in-memory store.
const _gmStore = {};
globalThis.GM_getValue = (key, def) => (key in _gmStore ? _gmStore[key] : def);
globalThis.GM_setValue = (key, val) => { _gmStore[key] = val; };

// Minimal location stub (init() calls getAdapter() → location.hostname).
globalThis.location = { hostname: "chatgpt.com", pathname: "/" };

// Minimal document stub (init() calls addEventListener).
globalThis.document = {
  addEventListener: () => {},
  createElement: () => ({
    style: {},
    textContent: "",
  }),
  body: { appendChild: () => {} },
  querySelectorAll: () => [],
  title: "Test Page",
};

// ── Load the userscript ────────────────────────────────────────────────────

const require = createRequire(import.meta.url);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.resolve(__dirname, "../userscript/localclip.user.js");

// The userscript calls init() at load time; our stubs above absorb that.
const {
  getConversationId,
  normalizeText,
  sha256hex,
  deriveMessageId,
  summarizeTurns,
} = require(scriptPath);

// ── Test runner ────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function test(description, fn) {
  try {
    const result = fn();
    // fn may return a Promise (async tests)
    if (result && typeof result.then === "function") {
      return result
        .then(() => {
          console.log(`  ✓ ${description}`);
          passed++;
        })
        .catch((err) => {
          console.error(`  ✗ ${description}`);
          console.error(`      ${err.message || err}`);
          failed++;
        });
    }
    console.log(`  ✓ ${description}`);
    passed++;
    return Promise.resolve();
  } catch (err) {
    console.error(`  ✗ ${description}`);
    console.error(`      ${err.message || err}`);
    failed++;
    return Promise.resolve();
  }
}

// ── getConversationId ──────────────────────────────────────────────────────

console.log("\ngetConversationId");

const tests = [];

tests.push(test("extracts UUID from /c/<uuid> path", () => {
  const result = getConversationId("/c/aaaabbbb-0000-0000-0000-000000000001");
  assert.equal(result, "aaaabbbb-0000-0000-0000-000000000001");
}));

tests.push(test("returns null for root path /", () => {
  assert.equal(getConversationId("/"), null);
}));

tests.push(test("returns null for /new (no conversation yet)", () => {
  assert.equal(getConversationId("/new"), null);
}));

tests.push(test("returns null for path without /c/ prefix", () => {
  assert.equal(getConversationId("/something/aaaabbbb-0000-0000-0000-000000000001"), null);
}));

tests.push(test("returns null for a partial / malformed UUID (too short)", () => {
  assert.equal(getConversationId("/c/aaaabbbb-0000"), null);
}));

tests.push(test("returns null for empty string", () => {
  assert.equal(getConversationId(""), null);
}));

tests.push(test("works when path has trailing segment after UUID", () => {
  const result = getConversationId("/c/aaaabbbb-0000-0000-0000-000000000001/extra");
  assert.equal(result, "aaaabbbb-0000-0000-0000-000000000001");
}));

tests.push(test("UUID must be lowercase hex (uppercase → null)", () => {
  // The pattern uses [0-9a-f] — uppercase letters should not match
  assert.equal(getConversationId("/c/AAAABBBB-0000-0000-0000-000000000001"), null);
}));

// ── normalizeText ──────────────────────────────────────────────────────────

console.log("\nnormalizeText");

tests.push(test("collapses multiple spaces to one", () => {
  assert.equal(normalizeText("hello   world"), "hello world");
}));

tests.push(test("collapses newlines and tabs", () => {
  assert.equal(normalizeText("hello\n\t\nworld"), "hello world");
}));

tests.push(test("trims leading and trailing whitespace", () => {
  assert.equal(normalizeText("  hello world  "), "hello world");
}));

tests.push(test("returns empty string for whitespace-only input", () => {
  assert.equal(normalizeText("   \n\t  "), "");
}));

tests.push(test("leaves already-normalized string unchanged", () => {
  assert.equal(normalizeText("hello world"), "hello world");
}));

tests.push(test("handles empty string", () => {
  assert.equal(normalizeText(""), "");
}));

// ── sha256hex ──────────────────────────────────────────────────────────────

console.log("\nsha256hex");

tests.push(test("produces correct hex digest for known input", async () => {
  // echo -n "hello" | sha256sum → 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
  const result = await sha256hex("hello");
  assert.equal(result, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
}));

tests.push(test("produces 64-character lowercase hex string", async () => {
  const result = await sha256hex("test input");
  assert.equal(typeof result, "string");
  assert.equal(result.length, 64);
  assert.match(result, /^[0-9a-f]{64}$/);
}));

tests.push(test("same input always produces same output", async () => {
  const a = await sha256hex("deterministic");
  const b = await sha256hex("deterministic");
  assert.equal(a, b);
}));

tests.push(test("different inputs produce different outputs", async () => {
  const a = await sha256hex("input one");
  const b = await sha256hex("input two");
  assert.notEqual(a, b);
}));

tests.push(test("empty string produces correct known digest", async () => {
  // echo -n "" | sha256sum → e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  const result = await sha256hex("");
  assert.equal(result, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
}));

// ── deriveMessageId ────────────────────────────────────────────────────────

console.log("\nderiveMessageId");

tests.push(test("uses attrId when present and non-empty", async () => {
  const result = await deriveMessageId("msg-abc123", "user", "some text");
  assert.equal(result, "msg-abc123");
}));

tests.push(test("uses attrId with leading/trailing whitespace stripped", async () => {
  const result = await deriveMessageId("  msg-abc123  ", "user", "some text");
  assert.equal(result, "msg-abc123");
}));

tests.push(test("falls back to sha256 when attrId is null", async () => {
  const result = await deriveMessageId(null, "user", "hello world");
  // Must be a 64-char hex string
  assert.match(result, /^[0-9a-f]{64}$/);
}));

tests.push(test("falls back to sha256 when attrId is empty string", async () => {
  const result = await deriveMessageId("", "user", "hello world");
  assert.match(result, /^[0-9a-f]{64}$/);
}));

tests.push(test("fallback hash is deterministic for same role+text", async () => {
  const a = await deriveMessageId(null, "user", "What is Python?");
  const b = await deriveMessageId(null, "user", "What is Python?");
  assert.equal(a, b);
}));

tests.push(test("fallback hash differs for different roles", async () => {
  const a = await deriveMessageId(null, "user",      "same text");
  const b = await deriveMessageId(null, "assistant", "same text");
  assert.notEqual(a, b);
}));

tests.push(test("fallback hash normalizes text before hashing", async () => {
  // Extra whitespace should be collapsed before hashing
  const a = await deriveMessageId(null, "user", "hello   world");
  const b = await deriveMessageId(null, "user", "hello world");
  assert.equal(a, b);
}));

tests.push(test("fallback hash includes role in input (separator is |)", async () => {
  // Verify the hash is sha256("user|hello world")
  const expected = await sha256hex("user|hello world");
  const result   = await deriveMessageId(null, "user", "hello world");
  assert.equal(result, expected);
}));

// ── summarizeTurns ─────────────────────────────────────────────────────────

console.log("\nsummarizeTurns");

tests.push(test("counts user turns correctly", () => {
  const messages = [
    { id: "u1", role: "user",      markdown: "Q1" },
    { id: "a1", role: "assistant", markdown: "A1" },
    { id: "u2", role: "user",      markdown: "Q2" },
    { id: "a2", role: "assistant", markdown: "A2" },
  ];
  const { userCount } = summarizeTurns(messages);
  assert.equal(userCount, 2);
}));

tests.push(test("returns the messages array unchanged", () => {
  const messages = [
    { id: "u1", role: "user",      markdown: "Q1" },
    { id: "a1", role: "assistant", markdown: "A1" },
  ];
  const { messages: out } = summarizeTurns(messages);
  assert.deepEqual(out, messages);
}));

tests.push(test("userCount is 0 for empty input", () => {
  const { userCount } = summarizeTurns([]);
  assert.equal(userCount, 0);
}));

tests.push(test("userCount is 0 when only assistant messages present", () => {
  const messages = [{ id: "a1", role: "assistant", markdown: "A1" }];
  const { userCount } = summarizeTurns(messages);
  assert.equal(userCount, 0);
}));

tests.push(test("userCount includes consecutive user messages", () => {
  const messages = [
    { id: "u1", role: "user", markdown: "Q1" },
    { id: "u2", role: "user", markdown: "Q2" },
    { id: "u3", role: "user", markdown: "Q3" },
  ];
  const { userCount } = summarizeTurns(messages);
  assert.equal(userCount, 3);
}));

// ── poll-gate logic (pure simulation) ─────────────────────────────────────

console.log("\npoll-gate (ackedCount simulation)");

tests.push(test("does not POST when userCount equals ackedCount", () => {
  // Simulate the gate: if userCount <= ackedCount, skip
  const ackedCount = 3;
  const { userCount } = summarizeTurns([
    { id: "u1", role: "user",      markdown: "Q1" },
    { id: "a1", role: "assistant", markdown: "A1" },
    { id: "u2", role: "user",      markdown: "Q2" },
    { id: "a2", role: "assistant", markdown: "A2" },
    { id: "u3", role: "user",      markdown: "Q3" },
    { id: "a3", role: "assistant", markdown: "A3" },
  ]);
  const shouldPost = userCount > ackedCount;
  assert.equal(shouldPost, false);
}));

tests.push(test("does POST when userCount exceeds ackedCount", () => {
  const ackedCount = 2;
  const { userCount } = summarizeTurns([
    { id: "u1", role: "user",      markdown: "Q1" },
    { id: "a1", role: "assistant", markdown: "A1" },
    { id: "u2", role: "user",      markdown: "Q2" },
    { id: "a2", role: "assistant", markdown: "A2" },
    { id: "u3", role: "user",      markdown: "Q3" },
    { id: "a3", role: "assistant", markdown: "A3" },
  ]);
  const shouldPost = userCount > ackedCount;
  assert.equal(shouldPost, true);
}));

tests.push(test("flush bypasses the ackedCount gate", () => {
  // flush=true means always POST regardless of count
  const flush = true;
  const ackedCount = 3;
  const { userCount } = summarizeTurns([
    { id: "u1", role: "user",      markdown: "Q1" },
    { id: "a1", role: "assistant", markdown: "A1" },
    { id: "u2", role: "user",      markdown: "Q2" },
    { id: "a2", role: "assistant", markdown: "A2" },
    { id: "u3", role: "user",      markdown: "Q3" },
    { id: "a3", role: "assistant", markdown: "A3" },
  ]);
  const shouldPost = flush || userCount > ackedCount;
  assert.equal(shouldPost, true);
}));

// ── @require URL guard ────────────────────────────────────────────────────

console.log("\n@require URL guard");

tests.push(test("@require URLs must not contain /dist/ (correct path is /lib/)", () => {
  const raw = fs.readFileSync(scriptPath, "utf-8");
  const requireLines = raw
    .split("\n")
    .filter((l) => l.trim().startsWith("// @require"));
  assert.ok(requireLines.length > 0, "No @require lines found in userscript");
  for (const line of requireLines) {
    assert.ok(
      !line.includes("/dist/"),
      `@require line uses /dist/ — should be /lib/:\n  ${line.trim()}`
    );
  }
}));

tests.push(test("turndown @require points to /lib/turndown.browser.umd.js", () => {
  const raw = fs.readFileSync(scriptPath, "utf-8");
  const expected = "https://cdn.jsdelivr.net/npm/turndown@7.2.4/lib/turndown.browser.umd.js";
  assert.ok(
    raw.includes(expected),
    `Expected @require URL not found:\n  ${expected}`
  );
}));

// ── Wait for all async tests, then report ─────────────────────────────────

Promise.all(tests).then(() => {
  const total = passed + failed;
  console.log(`\n${"─".repeat(50)}`);
  console.log(`${total} tests: ${passed} passed, ${failed} failed`);
  if (failed > 0) {
    console.error("\nSome tests failed.");
    process.exit(1);
  } else {
    console.log("\nAll tests passed ✓");
    process.exit(0);
  }
});
