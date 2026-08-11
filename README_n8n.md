# CAPTCHA alerts via n8n → Nodera

When `captcha_guard` sees a visible, unsolved CAPTCHA it parks the agent for
`CAPTCHA_WAIT_SECONDS` (currently 5 min) and POSTs to `N8N_CAPTCHA_WEBHOOK`. You VNC in
to display `:2`, click the checkbox, and the agent resumes on its own.

The alert is best-effort by design: if `N8N_CAPTCHA_WEBHOOK` is unset or n8n is down, the
run still pauses and still abandons the job on timeout. Only the notification is lost.

## How the notification reaches the phone

```
captcha_guard detects a CAPTCHA
  -> notify_captcha() POSTs JSON to N8N_CAPTCHA_WEBHOOK
    -> n8n Webhook node (responds immediately - the agent never blocks on n8n)
      -> Set node puts `message` on the execution output
        -> Nodera's notification node fires
          -> push on the phone
```

n8n has no push channel of its own. What makes this work is that **Nodera adds a
notification node to the workflow when you enable alerts on it**, so the push originates
from the execution itself rather than from the app checking in. Nodera is free, on Android
and iOS, and talks straight to the instance with a URL + API key.

## Events sent

Three, all POSTed as JSON:

| event | when |
|---|---|
| `captcha_blocked` | a challenge is up — this is the one you act on |
| `captcha_cleared` | you (or the widget) solved it, agent resumed |
| `captcha_timeout` | nobody solved it in time, job abandoned |

Every payload carries a ready-made `message` string plus the raw fields, so nothing needs
formatting inside n8n:

```json
{
  "event": "captcha_blocked",
  "message": "🔒 CAPTCHA on PNC. VNC to :2 and solve it - the agent waits 5 min.",
  "company": "PNC",
  "job_url": "https://pnc.wd5.myworkdayjobs.com/job/123",
  "page_url": "https://pnc.wd5.myworkdayjobs.com/login",
  "vnc_display": ":2",
  "wait_minutes": 5
}
```

## 1. Import and publish the workflow

Two nodes. Import `n8n_captcha_workflow.json` (next to this file) via **Workflows → ⋯ →
Import from File**, or build it by hand:

1. A **Webhook** node — method `POST`, path `captcha`, Respond **Immediately** (the agent
   must not block waiting on n8n).
2. An **Edit Fields (Set)** node after it named `Captcha Alert`, assigning `message`,
   `company`, `event`, `page_url` and `vnc_display` from `{{ $json.body.<field> }}`.

The Set node is what makes the alert readable in the app — it lifts the message out of the
raw webhook body onto the execution's output.

Then **publish it**. On n8n 2.x *saving does not activate the production webhook* — click
**Publish**. On older versions, flip the **Active** toggle instead.

This matters: the editor also shows a *test* URL (`/webhook-test/captcha`) that only works
while "Listen for test event" is open, expires after ~120 s, and dies after one call. Do
not put that one in `.env`.

Check it registered:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:5678/webhook/captcha -H 'Content-Type: application/json' -d '{}'
```

`200` means published. `404` means saved but not published.

## 2. Point the agent at it

Already in `.env`:

```sh
export N8N_CAPTCHA_WEBHOOK='http://127.0.0.1:5678/webhook/captcha'
```

**Localhost on purpose.** The agent runs on this same host, so going direct avoids
depending on DNS, Caddy and TLS being healthy at the exact moment a CAPTCHA fires. The n8n
UI will *display* your public `https://n8n.example.com/webhook/captcha` if the container
sets `WEBHOOK_URL` — same path, both work.

`main.py` reads this at startup, so **restart the agent after changing it**. The `flock`
guard in `run-browser-use.sh` means the running one has to stop first:

```sh
pkill -f 'run-browser-use.sh'; pkill -f 'python main.py'
setsid nohup ./run-browser-use.sh >> runner.log 2>&1 < /dev/null &
```

## 3. Connect Nodera

1. In n8n: **Settings → n8n API → Create an API key**. Copy it.
2. In Nodera: add the instance — base URL of your n8n install (*not* the webhook URL) and
   that API key. Self-hosted and self-signed certs are supported.
3. Enable notifications on the `browser-use captcha alert` workflow, trigger set to **all
   executions** — not errors-only, since a captcha alert is a *successful* run. Nodera adds
   its notification node to the workflow at this point.

If you ever give this workflow more than one exit path, duplicate Nodera's notification
node onto each one — it only sits on the branch it was added to. The current two-node
workflow has a single exit, so this is just something to remember.

## Test it end to end

Fires a realistic payload; your phone should buzz:

```sh
curl -X POST http://127.0.0.1:5678/webhook/captcha -H 'Content-Type: application/json' \
  -d '{"event":"captcha_blocked","message":"🔒 CAPTCHA on TestCo. VNC to :2 and solve it - the agent waits 5 min.","company":"TestCo","vnc_display":":2","page_url":"https://example.com"}'
```

To exercise the real Python path rather than a hand-rolled payload:

```sh
python -c "import asyncio, main; asyncio.run(main.notify_captcha('captcha_blocked', {'company':'E2E','job_url':'https://x'}, page_url='https://y'))"
```

## Tuning

In `main.py`: `CAPTCHA_WAIT_SECONDS` is how long the agent holds (raise it if 5 min is not
enough time to get to the VNC session), `CAPTCHA_POLL_SECONDS` is how often it re-checks
whether the challenge is gone. `CAPTCHA_MESSAGES` holds the wording of all three alerts.
