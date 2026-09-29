# Swan Lab

A small tool for Gray Swan Arena practice. You chat with an assistant model. It builds a test setup from
the criteria you paste, attacks a target model, judges each reply, and writes defenses (including a
hardened system prompt that it then re-tests).

## Start
Double-click `Start Swan Lab.cmd` (or run `py app.py`). A browser tab opens at http://127.0.0.1:8765.
Only the Python standard library is needed.

Copy `.env.example` to `.env` and fill in one (or both) of:
- `NANOGPT_KEY=...` to use models on [NanoGPT](https://nano-gpt.com).
- `LOCAL_BASE_URL=...` to use a model running on your own machine (see "Local models" below).

`.env` is gitignored. The key stays in the local server and never goes to the browser.

## Local models
Any server that speaks the OpenAI chat API works: [Ollama](https://ollama.com), LM Studio, llama.cpp's
`llama-server`, vLLM. Start it, then set its address in `.env`:

| Server | `LOCAL_BASE_URL` |
|---|---|
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| llama.cpp `llama-server` | `http://localhost:8080/v1` |
| vLLM | `http://localhost:8000/v1` |

(`LOCAL_KEY=...` is optional, for a server that checks keys.) Restart Swan Lab. At startup it lists the local
server's models, and they appear in the Models boxes as `local:<name>` (e.g. `local:qwen3:8b`).

- **Only a local server** (no `NANOGPT_KEY`): every model box goes to it, and the boxes start filled with its first
  model. Put different local models in different boxes if you like.
- **Both:** a model named `local:<name>` goes to your server; any other name goes to NanoGPT. So you can, for
  example, attack a local target with a NanoGPT assistant.

Local calls cost $0. The `web` tool uses NanoGPT's search, so it is off without a NanoGPT key. The assistant role
needs a fairly capable model (it writes its tool calls as JSON and follows a long prompt); small models do best as
the target or judge.

## Using it
- **Start a chat:** paste the Arena criteria (or pick a preset) and press Send. Each message gives the
  assistant up to "Steps per message" more steps.
- **Talk to it:** type instructions or questions at any time. If it's working, it reads your message at its
  next step. If it's waiting, your message starts it again. An empty Send means "keep going".
- **Chats:** the sidebar lists every saved chat, newest first. Click one to reopen it and read it, or type to carry on
  where it stopped (it keeps saving to the same file).
- **New chat:** starts fresh. The first message of every chat includes short summaries of the last 6 saved
  chats (scenario, approaches tried with verdicts, final summary), so it builds on earlier work.

## The assistant's tools
- `find_register(query, limit?)`: searches the bundled 18-genre Register Corpus and returns the closest
  register descriptions, structural features, specimens and phrasebook entries.
- `create_scenario(name, target_system_prompt, behavior, success_criteria, target_functions?)`: builds the setup from pasted
  criteria and saves it as a preset in `presets.json`. `target_functions` gives the target functions it can call (see below).
- `send_to_target(approach, path | message, new_conversation?, target?, erase_last?)`: sends a prompt file (`path`, word for word) or a typed `message` to the target (or,
  with two targets, the one named in `target`; switching starts a fresh chat) and returns its
  reply. `approach` is a short label. The code keeps a TRIED SO FAR list (label and verdict) and shows it
  after every step, so the assistant keeps trying new things.
- `write_defense(verdict, analysis, system_prompt?)`: records BROKE/HELD/PARTIAL plus notes. A
  `system_prompt` replaces the target's system prompt for later sends.
- `file(action, path, ...)`: list, read (paged with offset/limit; up to 2,000 lines or ~60,000 characters per read), search, write, append or edit files. It can read
  anything in this folder except hidden files (so never `.env`); it can write only inside `notes/`. It keeps its
  strategy notebook in `notes/strategy.md` (sent in full at the start of every chat) and updates it as it learns.
  Every chat also gets a folder, `notes/<chat id>/`, where it drafts each prompt as a `.md` file over several steps
  (outline, sections, reread, edits) before sending it; the opening message lists past chats' folders so it can reuse them.
  Its writes show as notes in the chat.
- `web(action, query | url)` (on by default; untick "Web search" under Models to turn it off): `search` finds real
  pages and returns excerpts of their text; `fetch` returns a page's full text (PDFs too, e.g. arXiv papers) 15,000 characters at a time, or up to 50,000 on request. Used like
  the Register Corpus, to borrow real documents' structure and phrasing. Paid from your NanoGPT balance (about
  $0.006 per search, $0.0015 per page); the cost shows in the top bar.
- `done(summary)`: stops and hands back to you, with a summary or an answer to your question.

## Target functions (agent scenarios)
The sidebar box "Functions the target can call" takes one function per line, e.g.
`transfer_funds(amount: number, to_account) — Send money from the customer's account` (types: number, int, bool; default
string). The functions do nothing: when the target calls one, the call and its arguments are shown under the reply
("Called transfer_funds({"amount": 900, ...})"), passed to the assistant and the judge, and the target is told "ok". So the
success criteria can say which function and which values count. Presets save their functions. Models with native tool
calling on NanoGPT (gpt-oss, glm-4.7-flash, nemotron, gemma) get real tools; models without it (llama-4-maverick) get the
functions in their system prompt and call them by writing `CALL name {...}`, labelled "functions as text".

## Files
- `planner_prompt.txt`: the assistant's instructions. Edit it freely; changes apply after a page refresh.
- `presets.json`: saved scenarios.
- `sessions/`: every chat, saved as `.json` and readable `.md`.
- `logs/calls.jsonl`: one line per model call with tokens, estimated cost and timing.
- `notes/`: the assistant's notebook (`notes/strategy.md`, created as it learns) and its prompt drafts.
- `test/fake_local_model.py`: a stand-in local server with canned replies, for trying the local setup without a GPU
  (see below).

## Trying the local setup without a GPU
`test/fake_local_model.py` pretends to be an Ollama server: it answers `/v1/models` and `/v1/chat/completions`
(streamed or not) with scripted replies, so the whole loop runs (the assistant creates a scenario, sends a probe,
judges it, and finishes) without any model.

```
py test/fake_local_model.py            # listens on http://127.0.0.1:11434/v1
```
Then set `LOCAL_BASE_URL=http://127.0.0.1:11434/v1` in `.env` (and no `NANOGPT_KEY`), run `py app.py`, pick a
preset and press Send.

Models (the NanoGPT defaults): the assistant is `z-ai/glm-5.3-flash-uncensored` with reasoning always on high (600 s timeout), and
the fallback is `qwen/qwen3.8-27b-uncensored`. The target (the defense) is `meta-llama/llama-4-maverick` (1000
max tokens, temperature 0), included in the NanoGPT subscription. Other included options: `openai/gpt-oss-120b`
(trained to rank the system prompt above the user), `nvidia/nemotron-3-ultra-550b-a55b` (largest),
`google/gemma-4-31b-it` (held its system prompt best in Balp-Straffon et al. 2026).

Second target: `openai/gpt-oss-120b` by default (blank = off). After a BROKE, the assistant retests the same idea on
the other target. Judge: `z-ai/glm-4.7-flash` (blank = off) grades every reply in parallel with the assistant; its
verdict shows as a "judge:" badge, in TRIED SO FAR, and as a separate tally in the saved .md.

Multi-turn (off by default; the top bar shows the mode): only you turn it on, by saying "multi-turn" in chat
("single-turn" or "one-shot" turns it off). Off, every send starts a fresh target
chat. On, the assistant may build an idea over several turns, crescendo-style, and use `erase_last` to remove a
refused turn from the target's history (PyRIT/Crescendo "backtracking"); erased turns are greyed out.
Target thinking: a thinking model's reasoning (e.g. gpt-oss) shows under each reply, and the assistant sees it too,
labelled as something the live Arena usually hides. "Target thinking effort" (Models) sets low/medium/high for thinking models.
First reply: if your opening message doesn't say single-turn or multi-turn, the assistant asks before acting.
A success doesn't stop the run: it retests on the other target and keeps going until the steps run out.
Progress: every 5 steps the assistant writes a short "Progress" note in the chat; its latest notes are fed back to it
each step (and to the next chat's summary if a run ends without one).
Live view: the assistant's steps are streamed, so its thinking and reply appear in the chat as they are written
(a live box that becomes the normal step entry when done). Stop ends the call at NanoGPT right away.
Long runs: Steps goes up to 500. Once a chat's history passes ~100k tokens, the assistant is sent only the opening
message, a note with your recent messages and all its progress notes, and the last ~20 steps; TRIED SO FAR shows the
latest 60 attempts plus a summary of older ones. Everything is still saved and shown in full. The run lives in the
browser tab, so keep the tab open and the laptop awake.
