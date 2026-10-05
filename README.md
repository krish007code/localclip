# LocalClip

Capture ChatGPT conversations turn by turn into a local Obsidian vault — with
`[[wiki links]]`, Prev/Next navigation, and topic hubs. Processed by a local
LLM (Ollama). Nothing leaves your machine.

## Quick start

```bash
# 1. Clone and enter the repo
git clone <repo-url> localclip
cd localclip

# 2. Create your personal config
cp config.example.json config.json

# 3. Generate a token — paste it in BOTH places shown below
openssl rand -hex 16

#   → open config.json and set:  "token": "<your-token>"
#   → open userscript/localclip.user.js and set:  const TOKEN = "<your-token>";

# 4. Install Ollama and pull a model (first time only)
#   https://ollama.com/download
ollama pull llama3.2

# 5. Start the LocalClip server
uv run python -m server.server

# 6. Install the userscript in Tampermonkey
#   a. Open Tampermonkey → Dashboard → click the + tab
#   b. Delete the placeholder code
#   c. Paste the entire contents of userscript/localclip.user.js
#   d. File → Save  (Ctrl+S)
#   e. Navigate to chatgpt.com — allow the 127.0.0.1 permission prompt

# 7. Capture a turn
#   Start a conversation, wait for ChatGPT to finish its reply,
#   then click the Tampermonkey icon → "Save now (flush)".
#   A toast "saved 1 turn(s)" confirms it worked.

# 8. Open the vault in Obsidian
#   File → Open Vault → point it at the  vault/  folder in this repo
```

> **Important**: always press **"Save now"** only after ChatGPT has finished
> its reply. Saving mid-stream captures an incomplete answer.

---

### Prerequisites
- [Tampermonkey](https://www.tampermonkey.net/) installed in Chrome, Edge, or Firefox.
- The LocalClip server running locally (see server setup below).

### Steps

1. **Open Tampermonkey** → click the Tampermonkey icon in your browser toolbar →
   **Dashboard**.

2. Click the **+** tab (or **Create a new script**).

3. **Delete** all the placeholder code in the editor.

4. Open `userscript/localclip.user.js` from this repo and **paste the entire
   contents** into the Tampermonkey editor.

5. **Set your token**: near the top of the script, find:
   ```js
   const TOKEN = "CHANGE_ME";
   ```
   Replace `"CHANGE_ME"` with the same random string you put in `config.json`.
   To generate one:
   ```bash
   openssl rand -hex 16
   ```

6. Click **File → Save** (or `Ctrl+S`).

7. Navigate to `https://chatgpt.com` — Tampermonkey will ask for permission to
   make requests to `127.0.0.1`. **Allow** it.

### First-run permission prompt

Tampermonkey shows a one-time confirmation dialog the first time
`GM_xmlhttpRequest` tries to reach `127.0.0.1`:

> *"LocalClip wants to make a request to 127.0.0.1. Allow?"*

Click **Always allow** so you don't see it again. Without this, every clip
will be blocked.

---

## Quick keyboard reference

| Key | Action |
|-----|--------|
| `Alt+S` | Clip immediately (sends all turns including the last pending one) |
| `Alt+P` | Toggle auto-capture on/off |

A small toast appears in the bottom-right corner confirming the result.

---

## Server setup (quick reference)

```bash
# 1. Clone and enter the repo
cd localclip

# 2. First-run: create your personal config
cp config.example.json config.json

# 3. Generate a token and set it in both places
openssl rand -hex 16
#   → paste the output into config.json  ("token" field)
#   → paste the same value into the userscript TOKEN constant

# 4. Start Ollama and pull a model (first time only)
ollama pull llama3.2

# 5. Start the LocalClip server
uv run python -m server.server

# 6. Open vault/ in Obsidian as a vault folder
```

---

## Changing the model

Edit one line in `config.json`:
```json
"model": "llama3.2"
```
Restart the server. No code changes needed.

---

## Re-rendering the vault

If you ever need to rebuild all vault files from scratch:
```bash
uv run python server/render.py --rebuild
```

---

## Re-processing queued clips (Ollama was offline)

If Ollama was down when you clipped, entries are saved as raw files and queued.
Once Ollama is back:
```bash
uv run python -m server.server --reprocess-queued
```

---

## Why local / open source

**Privacy**: your conversations never leave your machine. No API key, no cloud
account, no telemetry. The only network calls are browser → `127.0.0.1:8787`
and server → Ollama at `127.0.0.1:11434`.

**Zero cost**: Ollama is free. The models run on your own hardware. There is no
subscription, no per-token charge, and no rate limit.

**Offline use**: once the model is downloaded, LocalClip works with no internet
connection at all.

**Swap models freely**: change one line in `config.json` to use any model
Ollama supports — `mistral`, `phi3`, `gemma2`, or anything else. No code
changes, no retraining.
