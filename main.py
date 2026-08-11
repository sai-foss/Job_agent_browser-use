import os
import re
import sys
import json
import time
import asyncio
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, get_args

BASE = Path(__file__).parent

# Must be set before importing browser_use - it wires up file logging at import time.
os.environ.setdefault('BROWSER_USE_DEBUG_LOG_FILE', str(BASE / 'agent_debug.log'))

import httpx
import pandas as pd
from ollama import AsyncClient, ResponseError
from pydantic import BaseModel
from browser_use import Agent, Browser, ChatOllama, Tools
from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import UploadFileEvent
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.ollama.serializer import OllamaMessageSerializer
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.mcp.client import MCPClient

import job_scraper


VNC_DISPLAY = os.environ.get('VNC_DISPLAY', ':2')
VNC_XAUTHORITY = str(Path.home() / '.Xauthority')

os.environ['DISPLAY'] = VNC_DISPLAY
os.environ['XAUTHORITY'] = VNC_XAUTHORITY


# Both CSVs self-create, so neither needs to exist up front - but not symmetrically.
# progress.csv is written by ensure_progress() below; jobs.csv is written by
# job_scraper.scrape(). To start a fresh list, delete jobs.csv and run job_scraper.py
# directly: pending_jobs() reads it before the loop ever reaches the scrape, so main.py
# alone dies with FileNotFoundError. Deleting progress.csv is the dangerous one - it is
# the only record of what has been applied to, so the agent redoes every row it holds.
JOBS_CSV = str(BASE / 'jobs.csv')
PROGRESS_CSV = str(BASE / 'progress.csv')
AGENT_DIR = BASE / 'agent_dir'
# Whichever PDF sits in agent_dir is the resume, so no personal filename is baked into the
# repo. Falling back to a path that does not exist keeps the failure at first use, readable.
RESUME = str(next(iter(sorted(AGENT_DIR.glob('*.pdf'))), AGENT_DIR / 'resume.pdf'))
PERSONAL = str(AGENT_DIR / 'personal.md')
COVER = str(AGENT_DIR / 'cover_letter.md')
AGENT_EMAIL = os.environ['AGENT_EMAIL']
GMAIL_MCP_SCRIPT = str(BASE / 'gmail_mcp.py')

# qwen3.5 is the only model on this Ollama account that does BOTH things the agent needs:
# follows the AgentOutput schema (6/6 in testing) and accepts screenshots. Do not swap in
# deepseek/gpt-oss/glm/nemotron - they return HTTP 400 "does not support image input", and
# browser-use silently forces use_vision=False for deepseek, so it would run blind instead.
MODEL = 'qwen3.5:cloud'
# Multi-page ATS forms are long: a 6-section ADP application spent 39 steps and still ran out
# one checkbox short of submitting. Budget for finishing rather than for capping cost.
MAX_STEPS = 100
MAX_FAILURES = 12
CREDIT_RETRY_SECONDS = 600
IDLE_SECONDS = 1800

# Unset webhook just degrades to a console warning - a missing notifier must never be
# the thing that stops applications going out. Export N8N_CAPTCHA_WEBHOOK in .env with the
# n8n *production* webhook URL (see README_n8n.md); the test URL expires after one call.
N8N_CAPTCHA_WEBHOOK = os.environ.get('N8N_CAPTCHA_WEBHOOK', '')
CAPTCHA_WAIT_SECONDS = 300
CAPTCHA_POLL_SECONDS = 5

# Phrased here rather than in n8n so the workflow stays two nodes and one expression -
# any notifier that can send a string works without editing it.
CAPTCHA_MESSAGES = {
    'captcha_blocked': '🔒 CAPTCHA on {company}. VNC to {vnc_display} and solve it - the agent waits {wait_minutes} min.',
    'captcha_cleared': '🔓 CAPTCHA cleared on {company} - the agent is applying again.',
    'captcha_timeout': '⏰ CAPTCHA on {company} went unsolved after {wait_minutes} min - job abandoned.',
}

# The widget itself is a cross-origin iframe we cannot read into, but the response token
# it writes on success lands in the host document, so that is the "solved" signal.
# Only a *visible* challenge counts: reCAPTCHA v3 and managed Turnstile sit on the page
# permanently and solve themselves, and pausing 5 minutes on those would stall every job.
CAPTCHA_JS = """(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const token = ['#g-recaptcha-response', '[name="h-captcha-response"]', '[name="cf-turnstile-response"]']
    .map(s => document.querySelector(s)).find(el => el && el.value);
  const challenge = [
    'iframe[src*="recaptcha/api2/bframe"]',
    'iframe[src*="recaptcha/enterprise/bframe"]',
    'iframe[src*="hcaptcha.com"][src*="frame=challenge"]',
    'iframe[src*="challenges.cloudflare.com"]',
  ].flatMap(s => [...document.querySelectorAll(s)]).some(visible);
  return JSON.stringify({blocked: challenge && !token, solved: !!token});
})()"""

PROGRESS_COLUMNS = ['job_url', 'job_application_url_actual', 'company', 'progress_report', 'completed']

# Ollama reports quota exhaustion as 402/429 or a message naming the limit. Handled the same
# as a plain rate limit: park the run and re-probe until the quota comes back.
CREDIT_MARKERS = ('credit', 'quota', 'billing', 'insufficient', 'payment', 'upgrade', 'rate limit', 'limit reached')
out_of_credits = asyncio.Event()


def is_credit_error(e):
    return getattr(e, 'status_code', None) in (402, 429) or any(
        m in str(getattr(e, 'error', e)).lower() for m in CREDIT_MARKERS
    )


def _parse_json(text: str):
    """Pull the JSON object out of a reply that may be fenced or wrapped in prose.

    Ollama :cloud models ignore the `format` schema, so replies arrive as
    "Looking at the form, ...{json}" often enough to burn the whole failure budget.
    raw_decode also tolerates trailing prose.
    """
    s = re.sub(r'\s*```$', '', re.sub(r'^```(?:json)?\s*', '', text.strip()))
    return json.JSONDecoder().raw_decode(s, s.index('{'))[0]


class Params(NamedTuple):
    slot: str | None  # where a bare value goes, when that is unambiguous
    valid: frozenset[str]
    required: frozenset[str]


def _params_of(annotation) -> Params:
    model = next(
        (a for a in (get_args(annotation) or (annotation,)) if isinstance(a, type) and issubclass(a, BaseModel)),
        None,
    )
    if model is None:
        return Params(None, frozenset(), frozenset())
    fields = model.model_fields
    required = [p for p, f in fields.items() if f.is_required()]
    slot = required[0] if len(required) == 1 else next(iter(fields), None) if len(fields) == 1 else None
    return Params(slot, frozenset(fields), frozenset(required))


@lru_cache(maxsize=None)
def _action_params(output_format) -> dict[str, Params]:
    """action name -> its Params, read off the AgentOutput action model.

    Normally that model is a union of one-variant-per-action, but browser-use swaps in a
    plain single-action model whenever it restricts the step - done-only on the last step
    being the one that actually bites - so both shapes have to be handled.
    """
    action_model = get_args(output_format.model_fields['action'].annotation)[0]
    root = action_model.model_fields.get('root')
    variants = get_args(root.annotation) if root else (action_model,)
    return {
        name: _params_of(field.annotation)
        for variant in variants
        for name, field in variant.model_fields.items()
    }


def _coerce_actions(data, output_format):
    """Route a lone action parameter to the field it belongs to.

    qwen3.5 gets the envelope wrong in ways that each fail every variant of the union at
    once: it flattens single-parameter actions - {"click": 907} rather than
    {"click": {"index": 907}}, which cost a whole run of usable actions - and it invents
    plausible parameter names, {"wait": {"time": 3}} for "seconds", or
    {"select_dropdown": {"index": 3001, "value": "us"}} for "text". Every case is one
    value sitting in the wrong place, so each is repaired by moving that value to the one
    slot it can belong to. A value with more than one candidate slot is left to fail
    loudly rather than guessed at - a misrouted index clicks the wrong control.
    """
    if not isinstance(data, dict):
        return data
    try:
        spec = _action_params(output_format)
    except Exception:
        return data  # a shape we cannot read is a reason to skip repair, never to fail the step
    for action in data.get('action') or []:
        if not isinstance(action, dict):
            continue
        for name, params in action.items():
            p = spec.get(name)
            if p is None:
                continue
            if not isinstance(params, dict):
                action[name] = {p.slot: params} if p.slot else {} if not p.valid else params
                continue
            unknown = params.keys() - p.valid
            if not unknown:
                continue
            kept = {k: v for k, v in params.items() if k in p.valid}
            missing = p.required - kept.keys()
            target = next(iter(missing)) if len(missing) == 1 else p.slot if not p.required else None
            if len(unknown) == 1 and target and target not in kept:
                action[name] = kept | {target: params[next(iter(unknown))]}
            elif not missing:
                # Nothing to route it to and the call is already complete without it, so the
                # extra is invented context - a tab_id passed to get_latest_email, which the
                # task prompt invites by having the model note one right before the call.
                # Dropping is always safe: unlike a misrouted index it cannot act on anything.
                action[name] = kept
    return data


class TolerantChatOllama(ChatOllama):
    async def ainvoke(self, messages, output_format=None, **kwargs):  # type: ignore[override]
        if output_format is None:
            return await super().ainvoke(messages, None, **kwargs)
        try:
            response = await self.get_client().chat(
                model=self.model,
                messages=OllamaMessageSerializer.serialize_messages(messages),
                format=output_format.model_json_schema(),
                options=self.ollama_options,
            )
        except ResponseError as e:
            if is_credit_error(e):
                out_of_credits.set()
            raise ModelProviderError(message=str(e), model=self.name) from e
        try:
            data = _coerce_actions(_parse_json(response.message.content or ''), output_format)
            parsed = output_format.model_validate(data)
        except Exception as e:
            raise ModelProviderError(message=f'unparseable reply: {e}', model=self.name) from e
        return ChatInvokeCompletion(completion=parsed, usage=None)


llm = TolerantChatOllama(model=MODEL)


async def captcha_blocking(browser):
    """True when a visible CAPTCHA challenge is up and unsolved. False on any error -
    a flaky CDP eval must not be able to park the agent for five minutes."""
    try:
        session = await browser.get_or_create_cdp_session()
        result = await session.cdp_client.send.Runtime.evaluate(
            params={'expression': CAPTCHA_JS, 'returnByValue': True},
            session_id=session.session_id,
        )
        return json.loads(result['result']['value'])['blocked']
    except Exception:
        return False


async def notify_captcha(event, job, **extra):
    if not N8N_CAPTCHA_WEBHOOK:
        return
    payload = {
        'event': event,
        'company': job['company'],
        'job_url': job['job_url'],
        'vnc_display': VNC_DISPLAY,
        'wait_minutes': CAPTCHA_WAIT_SECONDS // 60,
        **extra,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(N8N_CAPTCHA_WEBHOOK, json=payload | {'message': CAPTCHA_MESSAGES[event].format(**payload)})
    except Exception as e:
        print(f'  n8n notify failed: {e}')


def captcha_guard(browser, job):
    """Build an on_step_start hook that parks the agent while a human clears a CAPTCHA.

    Detection sits here rather than in an agent tool because the model cannot be relied
    on to report a CAPTCHA it is already failing to solve. Blocking inside the hook is
    what pauses the run - the agent simply does not get handed its next step. The
    browser is headless=False on VNC_DISPLAY, so solving it means VNC in and click.
    """
    async def guard(agent):
        if not await captcha_blocking(browser):
            return
        # Managed Turnstile auto-solves in ~a second; re-check before crying wolf.
        await asyncio.sleep(CAPTCHA_POLL_SECONDS)
        if not await captcha_blocking(browser):
            return

        url = await browser.get_current_page_url()
        print(f'\n🔒 CAPTCHA at {url} - holding {CAPTCHA_WAIT_SECONDS // 60} min for a human')
        await notify_captcha('captcha_blocked', job, page_url=url)

        deadline = time.monotonic() + CAPTCHA_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(CAPTCHA_POLL_SECONDS)
            if not await captcha_blocking(browser):
                print('🔓 CAPTCHA cleared - resuming')
                await notify_captcha('captcha_cleared', job)
                return
        print('⏰ CAPTCHA unsolved - abandoning this job')
        await notify_captcha('captcha_timeout', job)
        agent.stop()

    return guard


def register_evaluate(tools):
    """Replace the built-in evaluate with one that repairs the two ways the model breaks it."""

    @tools.action(
        description='Run JavaScript in the current page and return its result. Use only for page/DOM '
        'work that click and input cannot do, e.g. a checkbox that will not respond to click. Agent '
        'actions are NOT JavaScript functions - never try to call one from in here.'
    )
    async def evaluate(code: str, browser_session: BrowserSession) -> ActionResult:
        # A lone call to something that is actually an action - get_latest_email(sender_contains='x'),
        # Python kwargs and all - burned a whole run of steps. Point it back at the real action.
        called = re.fullmatch(r'\s*(?:await\s+)?(\w+)\s*\([^()]*\)\s*;?\s*', code)
        if called and called[1] in tools.registry.registry.actions:
            return ActionResult(
                error=f'{called[1]} is an agent action, not a JavaScript function. Call it directly as '
                f'an action. evaluate only runs JavaScript inside the page.'
            )

        session = await browser_session.get_or_create_cdp_session()

        async def run(expression):
            return await session.cdp_client.send.Runtime.evaluate(
                params={'expression': expression, 'returnByValue': True, 'awaitPromise': True},
                session_id=session.session_id,
            )

        result = await run(code)
        # Runtime.evaluate takes an expression, so a top-level return or await is a SyntaxError -
        # and the model writes them constantly. Retrying wrapped beats wrapping on sight of the
        # keyword: a return inside a nested callback is already legal, and wrapping discards its value.
        if 'SyntaxError' in json.dumps(result.get('exceptionDetails', {})):
            result = await run(f'(async () => {{ {code} }})()')

        if details := result.get('exceptionDetails'):
            return ActionResult(error=f'JavaScript failed: {json.dumps(details.get("exception", details))[:300]}')

        value = result.get('result', {}).get('value')
        text = value if isinstance(value, str) else 'done' if value is None else json.dumps(value)
        return ActionResult(extracted_content=text, long_term_memory=f'evaluate: {text[:120]}')


def new_browser():
    """One Browser per job. Reusing a session across Agents does not work in 0.13.1:
    Agent.close() tears down the shared EventBus even with keep_alive=True, after which
    every BrowserStateRequestEvent returns None and the next agent fails instantly.
    user_data_dir persists, so logins carry over between jobs anyway."""
    return Browser(
        channel='chromium',
        user_data_dir=str(Path.home() / '.config/browser-use'),
        downloads_path=str(Path.home() / 'Downloads/browser-use'),
        headless=False,
        args=['--no-sandbox', '--disable-dev-shm-usage'],
        ## right now I dont want the headless chromium (I want to observe the agent at work)
    )


async def wait_for_credits():
    """Park until a one-token probe succeeds, then let the loop pick up where it stopped."""
    print(f'\n💤 Ollama credits exhausted - re-probing every {CREDIT_RETRY_SECONDS // 60} min')
    while True:
        await asyncio.sleep(CREDIT_RETRY_SECONDS)
        try:
            await AsyncClient().chat(
                model=MODEL, messages=[{'role': 'user', 'content': 'hi'}], options={'num_predict': 1}
            )
            out_of_credits.clear()
            print('✅ credits are back - resuming\n')
            return
        except Exception:
            pass


def build_task(company, job_url):
    return f"""
GOAL: Apply to the job at {company} and upload my resume.

START: Open {job_url} and begin the application process. You must click on the "Apply" link.

CREATE ACCOUNT:
1. If a login page appears, log in using username x_email and password x_pass.
2. If login fails (no existing account), sign up instead using username x_email and password x_pass.
3. If signup or login requires email verification, FIRST note the tab_id of the application tab from the
   browser state, then call get_latest_email. It takes ONLY sender_contains (the company or ATS name, to
   skip unrelated mail) and since_minutes - never pass it a tab_id or any other parameter. Then branch on
   what the email actually contains:
   a. IF it contains a verification CODE, type that code into the form and carry on. Do not open any link.
   b. ELSE IF it contains only a verification LINK ("Confirm your email", "Verify your account", "Activate"),
      open it with the navigate action passing new_tab: true. Wait for the confirmation page to load, then
      close that tab with the close action and return to the form with the switch action, passing the
      tab_id you noted above. Back on the application tab, click any "I have verified" / "Continue" button,
      or reload only if the form still blocks you.
      NEVER navigate the application tab itself to the verification link - that discards the half-filled
      form and the whole application has to be started over.
   c. IF it contains neither, the email has not arrived yet: use the wait action, then call get_latest_email
      again. Give up after 3 tries rather than looping.
4. If there is no sign up page proceed to the actual application.
5. If the application says "Job Closed" or "Unavailable in your region" Consider the Application Failed and STOP. 

DATA:
1. Use the read_file action to read {PERSONAL} once, before filling any fields. This is your source of truth for every personal detail needed to complete this application.
2. ALWAYS use the upload_resume action to attach my resume. NEVER click an "Attach resume" / "Upload" / "Choose file" button - that opens a native OS file dialog you cannot control and the run will stall.
3. Fill the form top to bottom, one field at a time: use input for text fields, click for dropdowns/checkboxes/radio buttons. Look up each value in {PERSONAL} before acting.
4. Do not skip a field just because it is optional - fill it if {PERSONAL} has a reasonable matching value.
5. If a field has no matching value in {PERSONAL} and you cannot reasonably infer one, leave it blank. Never invent values.
6. If the application asks for a cover letter use {COVER}

PROGRESS TRACKING: As soon as you land on the actual application page (after clicking Apply and any redirects), call log_application_started once, passing its progress_report parameter a short summary of what you are about to do. It records the current URL automatically.

CAPTCHA: A CAPTCHA is NOT a reason to stop. Challenges are watched for outside this task and a human
may clear one for you while you wait, so give that a chance to happen:
1. You may click an "I'm not a robot" checkbox ONCE. Never attempt an image, audio, or puzzle
   challenge yourself - you cannot pass it and every retry makes the block worse.
2. Otherwise use the wait action, then look at the page again. The challenge may simply be gone.
3. Only if it still blocks you after 3 such checks should you stop.

STOP: Once the job application has been submitted your work is done, or once you reach a step you cannot
complete (e.g. an account-creation wall, or a field you have no data for). Don't invent values for fields you can't fill.
"""


def ensure_progress():
    path = Path(PROGRESS_CSV)
    if not path.exists():
        pd.DataFrame(columns=PROGRESS_COLUMNS).to_csv(path, index=False)
        return
    df = pd.read_csv(path)
    if list(df.columns) != PROGRESS_COLUMNS:
        for col in PROGRESS_COLUMNS:
            if col not in df.columns:
                df[col] = ''
        df[PROGRESS_COLUMNS].to_csv(path, index=False)


def pending_jobs():
    """Jobs in jobs.csv with no progress row yet. Matching on job_url rather than row
    position keeps this correct as the scraper appends new listings between cycles.

    Requires jobs.csv to exist: this runs before the scrape in main()'s loop, so a
    clean checkout needs job_scraper.py run once first."""
    jobs = pd.read_csv(JOBS_CSV).to_dict('records')
    done = set(pd.read_csv(PROGRESS_CSV)['job_url'].dropna())
    return [j for j in jobs if j['job_url'] not in done]


def append_progress(job_url, url, company, report, completed):
    row = dict(zip(PROGRESS_COLUMNS, [job_url, url, company, report, completed]))
    pd.DataFrame([row]).to_csv(PROGRESS_CSV, mode='a', header=False, index=False)


def finalize_progress(rows_before, job_url, company, url, report, completed):
    """Guarantee exactly one progress row per job, whether or not the agent logged a start."""
    df = pd.read_csv(PROGRESS_CSV)
    if len(df) > rows_before:
        df.at[df.index[-1], 'progress_report'] = report
        df.at[df.index[-1], 'completed'] = completed
        df.to_csv(PROGRESS_CSV, index=False)
    else:
        append_progress(job_url, url, company, report, completed)


def rollback_progress(rows_before):
    """Drop a half-written row so a job interrupted by credit exhaustion is retried later."""
    df = pd.read_csv(PROGRESS_CSV)
    if len(df) > rows_before:
        df.iloc[:rows_before].to_csv(PROGRESS_CSV, index=False)


async def main():
    ensure_progress()

    # use the same password and email everywhere please
    sensitive_data: dict[str, str | dict[str, str]] = {
        'x_email': AGENT_EMAIL,
        'x_pass': os.environ['AGENT_PASSWORD'],
    }

    # evaluate is overwritten rather than dropped: it is the only thing that reaches the checkboxes
    # click() cannot, but it failed 261 times in one log on two repairable mistakes. Registering
    # over it must NOT be paired with exclude_actions=['evaluate'] - the exclusion also blocks the
    # replacement, and the agent silently ends up with no evaluate at all.
    tools = Tools()
    register_evaluate(tools)
    current = {'company': '', 'job_url': ''}

    @tools.action(
        description='Attach my resume to the resume/CV file field on the current page. It finds the file '
        'input itself and already knows the resume path - call it with no arguments. ALWAYS use this to '
        'upload the resume instead of clicking an "Attach resume" or "Upload" button, which opens a native '
        'OS dialog that cannot be controlled.'
    )
    async def upload_resume(
        browser_session: BrowserSession, file_path: str = '', path: str = '', index: int = -1
    ) -> ActionResult:
        # file_path/path/index are accepted then ignored on purpose. The model kept passing the
        # resume path it saw in the task, and a strict no-arg signature rejected every one of
        # those calls as extra_forbidden - 52 attempts, 0 uploads. Always upload RESUME.
        selector_map = await browser_session.get_selector_map()
        node = browser_session.find_file_input_near_element(selector_map[index]) if index in selector_map else None
        if node is None:
            node = next((n for n in selector_map.values() if browser_session.is_file_input(n)), None)
        if node is None:
            return ActionResult(error='No file input on this page yet - scroll to the resume section and retry.')

        event = browser_session.event_bus.dispatch(UploadFileEvent(node=node, file_path=RESUME))
        await event
        await event.event_result(raise_if_any=True, raise_if_none=False)
        return ActionResult(extracted_content='Resume uploaded', long_term_memory='Resume uploaded')

    @tools.action(
        description='Log that you have reached the real application page. Call this ONCE, right after Apply '
        'and any redirects. The current URL is recorded automatically - do not type it yourself.'
    )
    async def log_application_started(progress_report: str, browser_session: BrowserSession) -> str:
        url = await browser_session.get_current_page_url()
        append_progress(current['job_url'], url, current['company'], progress_report, False)
        return f'Logged application start at {url}'

    gmail_mcp = MCPClient(
        server_name='gmail',
        command=sys.executable,
        args=[GMAIL_MCP_SCRIPT],
        env={'GMAIL_ADDRESS': AGENT_EMAIL, 'GMAIL_APP_PASSWORD': os.environ['GMAIL_APP_PASSWORD']},
    )
    await gmail_mcp.connect()
    await gmail_mcp.register_to_tools(tools)

    async def should_stop():
        return out_of_credits.is_set()

    try:
        while True:
            pending = pending_jobs()
            if not pending:
                print('\n🔎 no jobs left - scraping for more')
                job_scraper.scrape()
                pending = pending_jobs()
                if not pending:
                    print(f'nothing new - sleeping {IDLE_SECONDS // 60} min')
                    await asyncio.sleep(IDLE_SECONDS)
                    continue

            print(f'\n{len(pending)} jobs to apply to')
            for n, job in enumerate(pending, start=1):
                if out_of_credits.is_set():
                    break
                current['company'], current['job_url'] = job['company'], job['job_url']
                rows_before = len(pd.read_csv(PROGRESS_CSV))
                print(f"\n=== {n}/{len(pending)}: {job['company']} ===")

                browser = new_browser()
                agent = Agent(
                    task=build_task(job['company'], job['job_url']),
                    llm=llm,
                    browser=browser,
                    tools=tools,
                    sensitive_data=sensitive_data,
                    available_file_paths=[RESUME, PERSONAL],
                    max_failures=MAX_FAILURES,
                    register_should_stop_callback=should_stop,
                )
                try:
                    history = await agent.run(max_steps=MAX_STEPS, on_step_start=captcha_guard(browser, job))
                    report = history.final_result() or 'no result'
                    completed = bool(history.is_successful())
                    visited = [u for u in history.urls() if u and u != 'about:blank']
                except Exception as e:
                    report, completed, visited = f'crashed: {type(e).__name__}: {e}', False, []
                finally:
                    await browser.kill()

                if out_of_credits.is_set():
                    rollback_progress(rows_before)  # leave this job pending for the retry
                    break

                finalize_progress(
                    rows_before, job['job_url'], job['company'],
                    visited[-1] if visited else job['job_url'], report, completed,
                )

            if out_of_credits.is_set():
                await wait_for_credits()
    finally:
        await gmail_mcp.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
