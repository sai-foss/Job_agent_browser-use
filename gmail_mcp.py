"""MCP server exposing read-only Gmail access so the agent can fetch verification codes.

Spawned as a subprocess over stdio by browser_use.mcp.client.MCPClient (see main.py).
Requires GMAIL_ADDRESS and GMAIL_APP_PASSWORD in the environment — generate an app
password at https://myaccount.google.com/apppasswords (needs 2-Step Verification on).
"""

import asyncio
import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime

from mcp.server.fastmcp import FastMCP

IMAP_HOST = 'imap.gmail.com'
# Without this the socket blocks forever, and Gmail does stall logins once the agent
# retries the tool a few times in a row - which is exactly what a verification wait does.
IMAP_TIMEOUT = 30
HEADERS_ONLY = '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])'
GMAIL_ADDRESS = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']

mcp = FastMCP('gmail')
# Gmail stalls logins that land at the same time, and every call opens its own connection.
# The agent asks one at a time anyway, so queueing costs nothing and removes the collision.
_mailbox = asyncio.Lock()


def _decode(header_value: str) -> str:
    parts = decode_header(header_value)
    return ''.join(part.decode(enc or 'utf-8', errors='ignore') if isinstance(part, bytes) else part for part, enc in parts)


def _body(msg: Message) -> str:
    if not msg.is_multipart():
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')
    for content_type in ('text/plain', 'text/html'):
        for part in msg.walk():
            if part.get_content_type() == content_type and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
    return ''


def _fetch(conn, msg_id: bytes, spec: str) -> Message | None:
    _, data = conn.fetch(msg_id, spec)
    return email.message_from_bytes(data[0][1]) if data and isinstance(data[0], tuple) else None


def _spam_folder(conn) -> str | None:
    """Gmail localises the spam folder name, so find it by its \\Junk special-use flag
    rather than hardcoding '[Gmail]/Spam', which only exists on English accounts."""
    _, folders = conn.list()
    for raw in folders or []:
        line = raw.decode(errors='ignore') if isinstance(raw, bytes) else str(raw)
        if '\\Junk' in line:
            return line.rsplit(' "/" ', 1)[-1].strip('"')
    return None


def _scan(conn, folder: str, sender_contains: str, cutoff: datetime) -> tuple[datetime, str] | None:
    """Newest message in one folder matching the filters, or None."""
    if conn.select(f'"{folder}"', readonly=True)[0] != 'OK':
        return None

    # IMAP SINCE only has day granularity; the real cutoff is enforced below via the Date header.
    _, data = conn.search(None, f'(SINCE "{cutoff.strftime("%d-%b-%Y")}")')

    # Headers first, body only for the one message we return. Pulling every full RFC822
    # body up front is what made a miss cost the whole day's mail in round trips.
    for msg_id in reversed(data[0].split()):
        head = _fetch(conn, msg_id, HEADERS_ONLY)
        try:
            received = parsedate_to_datetime(head['Date'])  # TypeError when head is None
        except (TypeError, ValueError):
            continue
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        # ids run oldest-first, so the first message past the cutoff ends the scan.
        # Skipping on instead walked the entire folder on every call that found nothing.
        if received < cutoff:
            break

        sender = _decode(head.get('From', ''))
        if sender_contains and sender_contains.lower() not in sender.lower():
            continue

        body = _fetch(conn, msg_id, '(BODY.PEEK[])')
        subject = _decode(head.get('Subject', ''))
        return received, (
            f'From: {sender}\nSubject: {subject}\nDate: {head["Date"]}\nFolder: {folder}'
            f'\n\n{_body(body).strip() if body else ""}'
        )
    return None


def _latest(sender_contains: str, since_minutes: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    with imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT) as conn:
        conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        # Spam is searched too: a brand-new address signing up to an ATS is exactly the
        # profile Gmail junks, and a verification mail the agent cannot see reads to it
        # as one that never arrived.
        folders = ['INBOX'] + ([spam] if (spam := _spam_folder(conn)) else [])
        hits = [hit for f in folders if (hit := _scan(conn, f, sender_contains, cutoff))]

    if not hits:
        return f'No matching emails in the last {since_minutes} min (searched {", ".join(folders)}).'
    return max(hits)[1]  # newest across folders


@mcp.tool()
async def get_latest_email(sender_contains: str = '', since_minutes: int = 15) -> str:
    """Fetch the most recent Gmail message, e.g. to read a signup/verification email.

    Args:
        sender_contains: only consider emails whose From header contains this (case-insensitive).
        since_minutes: only consider emails received within this many minutes.
    """
    # Off the event loop on purpose: FastMCP calls a sync tool inline, so one slow IMAP
    # socket froze the whole server and every later call timed out behind it.
    try:
        async with _mailbox:
            return await asyncio.to_thread(_latest, sender_contains, since_minutes)
    except (OSError, imaplib.IMAP4.error) as e:
        return f'Could not read the mailbox ({type(e).__name__}: {e}). Wait a few seconds and try again.'


if __name__ == '__main__':
    mcp.run(transport='stdio')
