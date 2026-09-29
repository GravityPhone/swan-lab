"""Swan Lab: a tiny local server for red-team practice runs.

The page (index.html + app.js) runs the planner loop. This server only:
  - serves the page,
  - forwards chat calls to NanoGPT with the key from .env (the key never reaches the browser),
    or to a local OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM) set by LOCAL_BASE_URL,
  - retries 429/5xx with backoff, logs usage/cost per call, and saves sessions to disk.

Run:  py app.py        (stdlib only, no installs)
"""
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = "https://nano-gpt.com/api/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_TRIES = 6

# Fallback prices [input, output] USD per million tokens; refreshed from /models at startup.
PRICES = {
    "z-ai/glm-5.3-flash-uncensored": [0.2, 0.8],
    "qwen/qwen3.8-27b-uncensored": [0.25, 1.5],
    "z-ai/glm-4.7-flash": [0.07, 0.4],
    "deepseek/deepseek-v4.1-flash": [0.1, 0.4],
    "deepseek/deepseek-v4-flash-latest": [0.05, 0.16],
    "deepseek/deepseek-v3.2": [0.28, 0.42],
    "deepseek/deepseek-v4-pro-0813": [1.1, 2.5],
    "deepseek/deepseek-v4-pro": [1.1, 2.2],
}
_log_lock = threading.Lock()
_save_lock = threading.Lock()


def load_env(name):
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*" + name + r"\s*=\s*(.+?)\s*$", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    return os.environ.get(name, "")


KEY = load_env("NANOGPT_KEY")
# A local OpenAI-compatible server, e.g. http://localhost:11434/v1 (Ollama) or http://localhost:1234/v1 (LM Studio).
LOCAL = load_env("LOCAL_BASE_URL").rstrip("/")
LOCAL_KEY = load_env("LOCAL_KEY") or "local"  # local servers want some key but ignore it


def route(model):
    """Which server a model name goes to: "local:<name>" always goes to LOCAL_BASE_URL; without a NanoGPT key,
    every model does. Returns (base url, key, model name to send, is local)."""
    if model.startswith("local:"):
        return LOCAL, LOCAL_KEY, model[len("local:"):], True
    if LOCAL and not KEY:
        return LOCAL, LOCAL_KEY, model, True
    return BASE, KEY, model, False


def list_local_models():
    """Add the local server's models (free) to the model list as local:<name>, so they show in the model boxes."""
    if not LOCAL:
        return
    try:
        with urllib.request.urlopen(LOCAL + "/models", timeout=10) as r:
            data = json.loads(r.read()).get("data", [])
        for m in data:
            PRICES["local:" + m["id"]] = [0, 0]
        print(f"local server {LOCAL}: {len(data)} models ({', '.join(m['id'] for m in data[:5])})")
    except Exception as e:
        print(f"local server {LOCAL} not reachable ({e}); start it, then restart Swan Lab")


def refresh_prices():
    if not KEY:
        return
    try:
        with urllib.request.urlopen(BASE + "/models?detailed=true", timeout=30) as r:
            data = json.loads(r.read()).get("data", [])
        for m in data:
            p = m.get("pricing") or {}
            if isinstance(p.get("prompt"), (int, float)) and isinstance(p.get("completion"), (int, float)):
                PRICES[m["id"]] = [p["prompt"], p["completion"]]
        print(f"prices refreshed for {len(data)} models")
    except Exception as e:  # keep the built-in table
        print(f"price refresh failed ({e}); using built-in prices")


def cost_of(model, usage):
    """List-price estimate from token counts. (The API's own usage.cost can be 0, so it is logged separately.)"""
    if not usage:
        return 0.0
    pin, pout = PRICES.get(model, [0, 0])
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1e6


def log_call(entry):
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with _log_lock, open(os.path.join(ROOT, "logs", "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


class ClientGone(Exception):
    """The page stopped listening (Stop pressed or tab closed) during a streamed call."""


def read_stream(resp, on_delta):
    """Read NanoGPT's server-sent events, pass each piece of thinking/reply to on_delta,
    and return the whole reply in the same shape as a non-streamed response."""
    content, reasoning, finish, usage = [], [], "", {}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: {"):
            continue
        d = json.loads(line[6:])
        usage = d.get("usage") or usage
        for c in d.get("choices") or []:
            delta = c.get("delta") or {}
            think = delta.get("reasoning") or delta.get("reasoning_content")
            if think:
                reasoning.append(think)
                on_delta("reasoning", think)
            if delta.get("content"):
                content.append(delta["content"])
                on_delta("content", delta["content"])
            finish = c.get("finish_reason") or finish
    return {"choices": [{"message": {"content": "".join(content), "reasoning": "".join(reasoning)}, "finish_reason": finish}],
            "usage": usage}


def chat(req, on_delta=None):
    """Forward one chat call. Returns a dict the page understands; never includes the key.
    With on_delta, the reply is streamed and each piece is passed to on_delta as it arrives."""
    model = req["model"]
    base, key, name, local = route(model)
    if not base:
        return {"ok": False, "error": f"{model} is a local model but LOCAL_BASE_URL is not set in .env."}
    if not key:
        return {"ok": False, "error": "No NANOGPT_KEY in .env next to app.py (or set LOCAL_BASE_URL to use a local model)."}
    body = {"model": name, "messages": req["messages"], "temperature": req.get("temperature", 0.0)}
    started = [False]  # True once anything has been streamed; after that a retry would repeat text on the page
    if on_delta:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        emit = on_delta

        def on_delta(kind, text):
            started[0] = True
            emit(kind, text)
    if req.get("max_tokens"):
        body["max_tokens"] = int(req["max_tokens"])
    if req.get("reasoning_effort"):
        body["reasoning_effort"] = req["reasoning_effort"]
    if req.get("tools"):
        body["tools"] = req["tools"]
    timeout = float(req.get("timeout", 90))
    data = json.dumps(body).encode()
    t0 = time.time()
    err = ""
    for attempt in range(1, MAX_TRIES + 1):
        r = urllib.request.Request(base + "/chat/completions", data=data, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                out = read_stream(resp, on_delta) if on_delta else json.loads(resp.read())
            choice = (out.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            usage = out.get("usage") or {}
            reasoning_tokens = usage.get("reasoning_tokens") or (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            result = {
                "ok": True, "model": model, "attempts": attempt,
                "content": msg.get("content") or "",
                "reasoning": msg.get("reasoning") or msg.get("reasoning_content") or "",
                "tool_calls": msg.get("tool_calls") or [],
                "finish_reason": choice.get("finish_reason") or "",
                "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                          "completion_tokens": usage.get("completion_tokens", 0),
                          "reasoning_tokens": reasoning_tokens or 0},
                "cost": 0.0 if local else cost_of(model, usage),
                "seconds": round(time.time() - t0, 1),
            }
            log_call({"t": datetime.now().isoformat(timespec="seconds"), "role": req.get("role", ""),
                      "model": model, **result["usage"], "cost": result["cost"], "api_cost": usage.get("cost"),
                      "finish_reason": result["finish_reason"], "seconds": result["seconds"], "attempts": attempt})
            return result
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:400]
            err = f"HTTP {e.code}: {text}"
            if e.code == 401 and not local:
                err += "  (key rejected; if the models list still works, the key is stale)"
            if e.code not in RETRY_STATUS:
                break
        except ClientGone:
            raise
        except Exception as e:  # timeouts, connection resets
            err = f"{type(e).__name__}: {e}"
            # A hung call already cost a full timeout; don't repeat it. The page tries the fallback model instead.
            if "timed out" in str(e).lower() or started[0]:
                break
        if attempt < MAX_TRIES:
            time.sleep(min(30, 1.5 * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2))
    log_call({"t": datetime.now().isoformat(timespec="seconds"), "role": req.get("role", ""),
              "model": model, "error": err[:200], "seconds": round(time.time() - t0, 1)})
    return {"ok": False, "error": err, "model": model, "seconds": round(time.time() - t0, 1)}


def save_preset(req):
    """Add a planner-built scenario to presets.json, replacing any preset with the same name."""
    name = str(req.get("name", "")).strip()[:120]
    system_prompt = str(req.get("system_prompt", "")).strip()
    if not name or not system_prompt:
        return {"ok": False, "error": "name and system_prompt are required"}
    path = os.path.join(ROOT, "presets.json")
    with _log_lock:
        with open(path, encoding="utf-8") as f:
            presets = json.load(f)
        presets = [p for p in presets if p.get("name") != name]
        preset = {"name": name, "scenario": str(req.get("scenario", "")).strip(), "system_prompt": system_prompt}
        if str(req.get("functions") or "").strip():
            preset["functions"] = str(req["functions"]).strip()
        presets.append(preset)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2)
    return {"ok": True, "presets": presets}


def history(limit=6):
    """Short digests of the most recent saved chats, so a new chat can build on earlier work."""
    folder = os.path.join(ROOT, "sessions")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):  # ids are timestamps, so newest first
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not st.get("stats", {}).get("sends"):
            continue  # nothing was tested
        # The latest summary or progress note, so a run that ran out of steps still passes on what it learned.
        done = [e.get("text", "") for e in st.get("timeline", []) if e.get("type") in ("done", "progress")]
        out.append({
            "id": name[:-5],
            "scenario": " ".join(str(st.get("scenario", "")).split())[:300],
            "tried": [f"{t.get('approach', '?')}{' @ ' + t['target'] if t.get('target') else ''} -> {t.get('verdict') or '?'}"
                      for t in st.get("tried", [])],
            "summary": " ".join(done[-1].split())[:600] if done else "",
        })
        if len(out) >= limit:
            break
    return out


def wins():
    """Every BROKE/PARTIAL attempt in the saved chats, newest first, rebuilt from each timeline
    so multi-turn wins keep all their turns (works for chats saved before this existed too)."""
    folder = os.path.join(ROOT, "sessions")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        timeline = st.get("timeline", [])
        built = [e.get("text", "").split("\n")[0] for e in timeline if e.get("type") == "built"]
        label = built[0] if built else " ".join(str(st.get("scenario", "")).split())[:80]
        convo, current, reply = [], None, ""
        for e in timeline:
            kind = e.get("type")
            if kind == "attack":
                if e.get("fresh"):
                    convo = []
                elif e.get("erase"):
                    convo = convo[:-1]  # that turn was erased from the target's history
                convo = convo + [e.get("text", "")]
                current, reply = {"approach": e.get("approach", "?"), "turns": list(convo)}, ""
            elif kind == "reply":
                reply = e.get("text", "")
            elif kind == "defense" and e.get("verdict") and current:
                if e["verdict"] in ("BROKE", "PARTIAL"):
                    out.append({"id": name[:-5], "scenario": label, "verdict": e["verdict"],
                                "reply": reply[:300], **current})
                current = None  # each attempt is judged once
    return out


# ---------- the assistant's web tool (experimental; the page turns it on) ----------
# search: NanoGPT's Linkup search, which returns page text rather than summaries.
# fetch: one page's full text via NanoGPT's scraper (PDFs too), 15k characters at a time or up to 50k on request.

WEB_PAGE = 15000    # characters per fetch unless the assistant asks for more
WEB_PAGE_MAX = 50000
_web_cache = {}  # url -> text, so paging through a page doesn't pay for it again


class _Text(HTMLParser):
    """HTML to plain text: drops scripts and styles, keeps paragraph breaks."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def page_text(s):
    if re.search(r"<(p|div|table|br|span|a)\b", s, re.I):  # the scraper's "markdown" is sometimes raw HTML
        parser = _Text()
        parser.feed(s)
        s = "".join(parser.out)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", s).strip()


def nano_post(path, body, timeout=60):
    r = urllib.request.Request("https://nano-gpt.com/api/" + path, data=json.dumps(body).encode(), method="POST",
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ValueError(f"NanoGPT {path} failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
    except OSError as e:
        raise ValueError(f"NanoGPT {path} failed: {e}")


def web_tool(req):
    """One call of the assistant's web tool. Returns (text for the assistant, cost in USD)."""
    action = str(req.get("action") or "search").lower().strip()
    if not KEY:
        raise ValueError("the web tool uses NanoGPT's search and needs NANOGPT_KEY in .env")
    t0 = time.time()
    if action == "search":
        query = str(req.get("query") or "").strip()
        if not query:
            raise ValueError("search needs a query")
        out = nano_post("web", {"query": query, "provider": "linkup", "depth": "standard", "outputType": "searchResults"})
        items = [i for i in (out.get("data") or []) if isinstance(i, dict)]
        n = max(1, min(10, int(req.get("limit") or 5)))
        cost = (out.get("metadata") or {}).get("cost") or 0
        text = f"{len(items)} results for {query!r} (showing {min(n, len(items))}; fetch a url for its full text):\n\n" + "\n\n".join(
            f"[{k}] {html.unescape(str(i.get('title', '')))}\n{i.get('url', '')}\n{page_text(str(i.get('content', '')))[:1500]}"
            for k, i in enumerate(items[:n], 1))
    elif action == "fetch":
        url = str(req.get("url") or "").strip()
        if not re.match(r"https?://", url):
            raise ValueError("fetch needs a full url starting with http:// or https://")
        cost = 0
        if url not in _web_cache:
            out = nano_post("scrape-urls", {"urls": [url]}, timeout=90)
            res = (out.get("results") or [{}])[0]
            if not res.get("success"):
                raise ValueError(f"could not fetch {url}: {res.get('error') or 'unknown error'}")
            _web_cache[url] = page_text(res.get("markdown") or res.get("content") or "")
            cost = (out.get("summary") or {}).get("actualCost") or 0
        text_all = _web_cache[url]
        start = max(0, int(req.get("offset") or 0))
        end = min(len(text_all), start + max(1000, min(WEB_PAGE_MAX, int(req.get("limit") or WEB_PAGE))))
        more = f"\n(more: fetch again with offset {end})" if end < len(text_all) else "\n(end of page)"
        text = f"{url} — characters {start}-{end} of {len(text_all)}:\n{text_all[start:end]}{more}"
    else:
        raise ValueError("action must be search or fetch")
    log_call({"t": datetime.now().isoformat(timespec="seconds"), "role": "web", "model": f"web:{action}",
              "cost": cost, "seconds": round(time.time() - t0, 1)})
    return text, cost


# ---------- the chat list ----------

_session_cache = {}  # file name -> (mtime, summary); a chat's JSON is only re-read when it changes


def sessions():
    """One line per saved chat for the sidebar, newest first."""
    folder = os.path.join(ROOT, "sessions")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        mtime = os.path.getmtime(path)
        cached = _session_cache.get(name)
        if not cached or cached[0] != mtime:
            try:
                with open(path, encoding="utf-8") as f:
                    st = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            stats = st.get("stats", {})
            cached = (mtime, {"id": name[:-5], "title": " ".join(str(st.get("scenario", "")).split())[:90] or "(empty chat)",
                              "steps": stats.get("steps", 0), "sends": stats.get("sends", 0),
                              "broke": stats.get("BROKE", 0), "partial": stats.get("PARTIAL", 0)})
            _session_cache[name] = cached
        out.append(cached[1])
    return out


def session(sid):
    sid = re.sub(r"[^0-9A-Za-z_-]", "", sid)[:60]
    with open(os.path.join(ROOT, "sessions", sid + ".json"), encoding="utf-8") as f:
        return json.load(f)


# ---------- the assistant's file tool ----------
# Reads anywhere in the Swan Lab folder except hidden files (.env holds the key); writes only inside notes/.

REAL_ROOT = os.path.realpath(ROOT)
TEXT_EXT = (".md", ".txt", ".json", ".jsonl", ".csv", ".py", ".js", ".html", ".cmd")


def safe_path(path, write=False):
    rel = str(path or ".").replace("\\", "/").strip().lstrip("/") or "."
    full = os.path.realpath(os.path.join(REAL_ROOT, rel))
    if full != REAL_ROOT and not full.startswith(REAL_ROOT + os.sep):
        raise ValueError("that path is outside the Swan Lab folder")
    rel = os.path.relpath(full, REAL_ROOT).replace(os.sep, "/")
    parts = [] if rel == "." else rel.split("/")
    if any(p.startswith(".") or p == "__pycache__" for p in parts):
        raise ValueError("hidden files are off limits")
    if write and (len(parts) < 2 or parts[0] != "notes"):
        raise ValueError("you can only write inside notes/, e.g. notes/strategy.md")
    return full, rel


def file_list(full, rel):
    if not os.path.isdir(full):
        raise ValueError(f"{rel} is not a folder")
    rows = []
    for name in os.listdir(full):
        if name.startswith(".") or name == "__pycache__":
            continue
        p = os.path.join(full, name)
        st = os.stat(p)
        rows.append((not os.path.isdir(p), -st.st_mtime, name, st.st_size, st.st_mtime))
    rows.sort()
    lines = [f"{name}/" if not is_file else f"{name}  ({size:,} bytes, {datetime.fromtimestamp(mt):%Y-%m-%d %H:%M})"
             for is_file, _, name, size, mt in rows[:100]]
    more = f"\n(showing 100 of {len(rows)})" if len(rows) > 100 else ""
    return f"{rel}/ — {len(rows)} entries, folders first, newest files first:\n" + "\n".join(lines) + more


def file_read(full, rel, offset, limit):
    if not os.path.isfile(full):
        raise ValueError(f"{rel} is not a file (use list to see what exists)")
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    start = max(1, int(offset or 1))
    end = min(len(lines), start - 1 + max(1, min(2000, int(limit or 300))))
    out, size = [], 0
    for i, l in enumerate(lines[start - 1:end], start):
        row = f"{i}\t{l[:8000]}{' …(line cut)' if len(l) > 8000 else ''}"
        if out and size + len(row) > 60000:  # about 60k characters per read, whatever the line count
            end = i - 1
            break
        out.append(row)
        size += len(row) + 1
    body = "\n".join(out)
    more = f"\n(more below: read again with offset {end + 1})" if end < len(lines) else "\n(end of file)"
    return f"{rel} — lines {start}-{end} of {len(lines)}:\n{body}{more}"


def file_search(full, rel, query, offset):
    if not query:
        raise ValueError("search needs a query")
    try:
        pat = re.compile(query, re.I)
    except re.error:
        pat = re.compile(re.escape(query), re.I)
    files = [full] if os.path.isfile(full) else [
        os.path.join(d, n) for d, dirs, names in os.walk(full)
        for n in names if n.endswith(TEXT_EXT) and not n.startswith(".")
        if not (n.endswith(".json") and n[:-5] + ".md" in names)  # a session's .md already has its text
        if not any(p.startswith(".") or p == "__pycache__" for p in os.path.relpath(d, REAL_ROOT).split(os.sep))]
    files.sort(key=os.path.getmtime, reverse=True)  # newest first: recent work is usually what matters
    hits, per_file = [], []
    for p in files:
        name = os.path.relpath(p, REAL_ROOT).replace(os.sep, "/")
        n = 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if pat.search(line):
                    hits.append(f"{name}:{i}: {line.strip()[:300]}")
                    n += 1
        if n:
            per_file.append(f"  {name} — {n}")
    skip = max(0, int(offset or 0))
    page = hits[skip:skip + 60]
    more = f"\n(more matches: search again with offset {skip + 60})" if skip + 60 < len(hits) else ""
    head = f"{len(hits)} matching lines for {query!r} in {len(per_file)} files, newest first:\n" + "\n".join(per_file[:15])
    if len(per_file) > 15:
        head += f"\n  … and {len(per_file) - 15} more files"
    return head + "\n\nMatches:\n" + ("\n".join(page) or "(none)") + more


def file_tool(req):
    """One call of the assistant's file tool. Returns text for the assistant, or raises ValueError."""
    action = str(req.get("action") or "").lower().strip()
    if action not in ("list", "read", "search", "write", "append", "edit"):
        raise ValueError("action must be list, read, search, write, append or edit")
    writing = action in ("write", "append", "edit")
    full, rel = safe_path(req.get("path") or ("notes/strategy.md" if writing else "."), write=writing)  # notes default to the notebook
    if action == "list":
        return file_list(full, rel)
    if action == "read" and req.get("raw"):  # the page's own read: exact text, e.g. a prompt file to send
        if not os.path.isfile(full):
            raise ValueError(f"{rel} is not a file (use list to see what exists)")
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()
    if action == "read":
        return file_read(full, rel, req.get("offset"), req.get("limit"))
    if action == "search":
        return file_search(full, rel, str(req.get("query") or ""), req.get("offset"))
    content = str(req.get("content") or "")
    if action in ("write", "append"):
        if len(content) > 200_000:
            raise ValueError("that is too long (200 KB max)")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if action == "append" and os.path.exists(full):
            with open(full, encoding="utf-8") as f:
                old = f.read()
            content = old + ("" if not old or old.endswith("\n") else "\n") + content
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"{'appended to' if action == 'append' else 'wrote'} {rel} (now {len(content.splitlines())} lines)"
    old, new = str(req.get("old") or ""), str(req.get("new") or "")
    if not os.path.isfile(full):
        raise ValueError(f"{rel} does not exist yet; use write to create it")
    with open(full, encoding="utf-8") as f:
        text = f.read()
    n = text.count(old) if old else 0
    if n == 0:
        raise ValueError("old text not found; read the file and copy the exact text")
    if n > 1 and str(req.get("replace_all")).lower() != "true":
        raise ValueError(f"old text appears {n} times; include more of the surrounding text, or set replace_all: true")
    with open(full, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))
    return f"edited {rel} ({n} replacement{'s' if n > 1 else ''})"


def read_text(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_chat(self, req):
        """Send the reply to the page as it is written: one JSON line per piece, then a final result line."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def line(obj):
            try:
                self.wfile.write((json.dumps(obj) + "\n").encode())
                self.wfile.flush()
            except OSError:
                raise ClientGone()

        try:
            result = chat(req, lambda kind, text: line({"type": "delta", "kind": kind, "text": text}))
            line({"type": "done", "result": result})
        except ClientGone:
            log_call({"t": datetime.now().isoformat(timespec="seconds"), "role": req.get("role", ""),
                      "model": req.get("model"), "error": "stopped by the page mid-stream"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.send(200, read_text("index.html"), "text/html")
        if path == "/app.js":
            return self.send(200, read_text("app.js"), "text/javascript")
        if path == "/api/config":
            with open(os.path.join(ROOT, "presets.json"), encoding="utf-8") as f:
                presets = json.load(f)
            with open(os.path.join(ROOT, "registers.json"), encoding="utf-8") as f:
                registers = json.load(f)
            return self.send(200, {"keyLoaded": bool(KEY or LOCAL), "webAvailable": bool(KEY), "local": LOCAL,
                                   "prices": PRICES,
                                   "plannerPrompt": read_text("planner_prompt.txt"), "presets": presets,
                                   "registers": registers})
        if path == "/api/history":
            return self.send(200, history())
        if path == "/api/wins":
            return self.send(200, wins())
        if path == "/api/sessions":
            return self.send(200, sessions())
        if path == "/api/session":
            sid = self.path.partition("id=")[2]
            try:
                return self.send(200, session(sid))
            except (OSError, json.JSONDecodeError):
                return self.send(404, {"error": "no such chat"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only accept same-origin calls so other sites can't use the local key.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send(403, {"error": "bad origin"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self.send(400, {"error": "bad json"})
        if self.path == "/api/chat" and req.get("stream"):
            return self.stream_chat(req)
        if self.path == "/api/chat":
            return self.send(200, chat(req))
        if self.path == "/api/web":
            try:
                text, cost = web_tool(req)
                return self.send(200, {"ok": True, "text": text, "cost": cost})
            except ValueError as e:
                return self.send(200, {"ok": False, "error": str(e)})
        if self.path == "/api/file":
            try:
                return self.send(200, {"ok": True, "text": file_tool(req)})
            except (ValueError, OSError) as e:
                return self.send(200, {"ok": False, "error": str(e)})
        if self.path == "/api/preset":
            return self.send(200, save_preset(req))
        if self.path == "/api/save":
            sid = re.sub(r"[^0-9A-Za-z_-]", "", str(req.get("id", "")))[:60] or "session"
            os.makedirs(os.path.join(ROOT, "sessions"), exist_ok=True)
            # One save at a time, each written to a temp file and swapped in, so two overlapping saves
            # (the judge saves while the loop does) can't leave a file half one and half the other.
            with _save_lock:
                for ext, text in ((".json", json.dumps(req.get("state", {}), indent=1)), (".md", req.get("markdown", ""))):
                    path = os.path.join(ROOT, "sessions", sid + ext)
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(path + ".tmp", path)
            return self.send(200, {"ok": True, "file": f"sessions/{sid}.md"})
        self.send(404, {"error": "not found"})


def main():
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8765)
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("no free port")
    list_local_models()
    threading.Thread(target=refresh_prices, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    where = ("NanoGPT key loaded" if KEY else "") + (" + " if KEY and LOCAL else "") + (f"local models at {LOCAL}" if LOCAL else "")
    print(f"Swan Lab running at {url}  ({where or 'no model source - add NANOGPT_KEY or LOCAL_BASE_URL to .env'})")
    print("Close this window or press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
