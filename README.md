

https://github.com/user-attachments/assets/3ac5658d-95b1-43a5-a67c-a6beea7ccd52

# Autonomous job application agent

A long-running agent that scrapes job listings, scores them against your resume, and then
fills in and submits the applications itself — driving a real Chromium window with
[browser-use](https://github.com/browser-use/browser-use) and a local/cloud
[Ollama](https://ollama.com) model. It creates ATS accounts, reads its own verification
emails over IMAP, uploads your resume, and parks itself for a human when it hits a CAPTCHA.

It runs unattended for days. Everything it has done lives in `progress.csv`, one row per job.

> Applying to jobs with an automated agent is against the terms of service of some job
> boards and ATS platforms. It also submits real applications to real employers under your
> name. Read what it produced before you rely on it, and run it on listings you actually want.

## Demo

One job end to end: the supervisor starts, the agent opens a LinkedIn listing, follows it
through to Netflix's ATS, uploads the resume, and works down the form — contact information,
self-ID questions, additional documents, application questions — to the confirmation screen.
Played at 4x; the run itself took 4m45s.

<video src="https://github.com/sai-foss/Job_agent_browser-use/releases/download/v0.1.0/example_video_an.mp4" controls muted width="100%"></video>

If the player above does not load, the recording is in the repo:
[`example_video_an.mp4`](example_video_an.mp4).

## How it works

```
job_scraper.py                    main.py
  jobspy → LinkedIn                 pending_jobs()  = jobs.csv − progress.csv
  scrape each search term             │
  rate 1-10 vs. your resume           ├─ one Browser + Agent per job
  keep >= MIN_RATING                  │    task: open listing → Apply → sign up / log in
  merge into jobs.csv ────────────────┘    │  → get_latest_email (Gmail MCP) for the code
                                           │  → read personal.md → fill fields
                                           │  → upload_resume
                                           │  → submit
                                           ├─ captcha_guard hook: pause, push alert, wait
                                           └─ append result to progress.csv
```

Two CSVs carry all the state. `jobs.csv` is the queue, `progress.csv` is the record of what
has been attempted; the difference between them is the work left to do. Matching is on
`job_url`, so the scraper can append new listings mid-run without disturbing anything.

**Deleting `progress.csv` makes the agent re-apply to every job in `jobs.csv`.** It is the
only record that exists. Deleting `jobs.csv` alone is safe.

## What is actually interesting here

Most of this repo is the gap between "an LLM can drive a browser" and "an LLM can finish a
40-step application form without help". The load-bearing parts:

- **`TolerantChatOllama`** — Ollama `:cloud` models ignore the `format` JSON schema, so
  replies arrive wrapped in prose and fail structured parsing. This subclass digs the JSON
  out of the prose and repairs the two envelope mistakes the model makes constantly:
  flattening single-parameter actions (`{"click": 907}` instead of `{"click": {"index": 907}}`)
  and inventing plausible-but-wrong parameter names. A value with more than one candidate
  slot is left to fail loudly rather than guessed at — a misrouted index clicks the wrong control.
- **`captcha_guard`** — detection sits in an `on_step_start` hook, not an agent tool,
  because a model that is failing a CAPTCHA cannot be relied on to report it. Blocking
  inside the hook is what pauses the run. It only fires on a *visible* challenge, since
  reCAPTCHA v3 and managed Turnstile sit on every page and solve themselves.
- **A replaced `evaluate` action** — the built-in one failed 261 times in a single log on
  two repairable mistakes: a top-level `return`/`await` (illegal in `Runtime.evaluate`,
  retried wrapped in an async IIFE) and calling an agent action as if it were a JS function.
- **`upload_resume`** — accepts and ignores `file_path`/`path`/`index`. A strict no-argument
  signature rejected 52 consecutive calls as `extra_forbidden` because the model insisted on
  passing the path it saw in the task. Always uploads the configured resume.
- **A Gmail MCP server** (`gmail_mcp.py`) — read-only IMAP, headers-first so a miss costs a
  handful of round trips rather than the day's mail, and it searches Spam too: a brand-new
  address signing up to an ATS is exactly the profile Gmail junks.
- **One `Browser` per job** — reusing a session across agents does not work in browser-use
  0.13.1; `Agent.close()` tears down the shared EventBus even with `keep_alive=True`.
  `user_data_dir` persists, so logins carry over anyway.

## Requirements

- Linux with a display for the browser to appear on. It runs `headless=False` on purpose so
  you can watch and take over — a VNC-backed `:2` in this setup, but any X display works.
- Python 3.12+
- Chromium, and Ollama with a vision-capable model.
- A **throwaway Gmail account** with 2-Step Verification and an app password. Do not use a
  personal address; every ATS the agent touches will mail it forever.

## Setup

```sh
git clone <this repo> && cd browser-use
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in the four values
cp agent_dir/personal.example.md agent_dir/personal.md
```

Then put your files in `agent_dir/` (git-ignored in full — nothing in it is ever tracked):

| file | what it is |
|---|---|
| `personal.md` | every value the agent is allowed to type into a form. **Its only source of truth** — it is instructed never to invent a field, so anything missing is left blank. |
| `<anything>.pdf` | your resume. The one PDF in `agent_dir` is picked up automatically. |
| `cover_letter.md` | pasted into cover-letter boxes when a form asks. |

Set your model in `main.py`. `MODEL` must both follow the `AgentOutput` schema *and* accept
screenshots — deepseek/gpt-oss/glm/nemotron return HTTP 400 on image input, and browser-use
silently sets `use_vision=False` for deepseek, so the agent would run blind rather than fail.

## Running

The scraper has to run once before the agent, because `pending_jobs()` reads `jobs.csv`
before the loop ever reaches a scrape:

```sh
python job_scraper.py
```

Then, for a single foreground run:

```sh
python main.py
```

Or supervised, which is how it is meant to run — `flock` so only one agent is ever live,
leftover Chromium reaped, restarted on hard crashes:

```sh
setsid nohup ./run-browser-use.sh >> runner.log 2>&1 < /dev/null &
```

To stop it:

```sh
pkill -f 'run-browser-use.sh'; pkill -f 'python main.py'
```

`main.py` never exits on its own. Out of Ollama credits it parks and re-probes every 10
minutes; out of jobs it scrapes for more, and sleeps 30 minutes if there are none.

> `run-browser-use.sh` kills **every** chrome/chromium process on the box, not just the
> agent's — including your personal browsing session. Narrow the `pkill` pattern to
> `user-data-dir=…/browser-use` if that matters to you.

## CAPTCHAs

The agent cannot solve them and does not try. On a visible unsolved challenge it pauses for
`CAPTCHA_WAIT_SECONDS` (5 min), POSTs to `N8N_CAPTCHA_WEBHOOK`, and resumes by itself the
moment the challenge clears — so you VNC in, click the checkbox, and walk away. Unsolved at
the deadline, the job is abandoned and the loop moves on.

The webhook is best-effort: unset it, or let n8n go down, and the agent still pauses and
still times out correctly. Only the notification is lost. **[README_n8n.md](README_n8n.md)**
covers the n8n workflow and phone push setup.

## Tuning

Everything worth changing sits at the top of the two modules.

| | |
|---|---|
| `main.py` `MAX_STEPS` | 100. A 6-section ADP form once spent 39 steps and still ran out one checkbox short. Budget for finishing, not for capping cost. |
| `main.py` `CAPTCHA_WAIT_SECONDS` | how long a human has to get to the VNC session. |
| `main.py` `build_task()` | the agent's instructions. Most behaviour changes belong here. |
| `job_scraper.py` `SEARCH_TERMS`, `LOCATION` | what gets scraped. |
| `job_scraper.py` `MIN_RATING` | 4/10. Unrated jobs are kept, so an Ollama outage cannot silently empty the queue. |

## Layout

```
LICENSE                     MIT
main.py                     agent loop, CAPTCHA guard, custom actions, Ollama repair layer
job_scraper.py              jobspy scrape + LLM resume-fit scoring → jobs.csv
gmail_mcp.py                read-only Gmail MCP server (stdio subprocess)
run-browser-use.sh          supervisor: flock, browser cleanup, restart loop
n8n_captcha_workflow.json   importable two-node n8n workflow for CAPTCHA push alerts
agent_dir/                  your resume, personal.md, cover letter — git-ignored
jobs.csv / progress.csv     the queue and the record — git-ignored
```

## License

[MIT](LICENSE)
