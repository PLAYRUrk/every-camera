"""Outgoing mail: the sender account and the spool. Standard library only.

A measurement thread must never open a socket: a slow SMTP server would hold up a
frame, a hung one the night. Workers only write a JSON file into
``~/.every_camera/outbox``; ``sentinel.py``, a separate process, sends it, so a letter
queued a second before the program dies still goes out.

The account stays out of ``config.json``, so setting a station up means typing the
addresses to notify and nothing else. Looked for in this order:

    ~/.every_camera/mail_account.json   outside the checkout, so neither a commit nor
                                        ``update.py --force`` can touch it
    <checkout>/mail_account.json        beside the program, like ``env.sh``
    config.json ``alerts.smtp``         for a station that needs its own mailbox
"""
import json
import os
import smtplib
import socket
import ssl
import time
import uuid

from datetime import datetime as dt
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = Path.home() / ".every_camera"
SPOOL_DIR = str(HOME_DIR / "outbox")
QUOTA_FILE = str(HOME_DIR / "mail_quota.json")
ACCOUNT_FILES = (
    str(HOME_DIR / "mail_account.json"),
    os.path.join(APP_DIR, "mail_account.json"),
)

# "starttls" connects in the clear and upgrades; "none" is for a relay on localhost.
SMTP_PRESETS = {
    "yandex": {"host": "smtp.yandex.ru", "port": 465, "security": "ssl"},
    "mailru": {"host": "smtp.mail.ru", "port": 465, "security": "ssl"},
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
    "brevo": {"host": "smtp-relay.brevo.com", "port": 587, "security": "starttls"},
    "resend": {"host": "smtp.resend.com", "port": 465, "security": "ssl"},
    "localhost": {"host": "localhost", "port": 25, "security": "none"},
    "custom": {},
}

PRESET_HINTS = {
    "yandex": "Yandex needs an application password (id.yandex.ru → Security → "
              "App passwords → Mail), not the account password.",
    "mailru": "Mail.ru needs an application password (Security → App passwords), "
              "not the account password.",
    "gmail": "Gmail needs an app password, which requires 2-step verification "
             "on the account.",
    "brevo": "Brevo's SMTP login is not the account e-mail: take both from "
             "SMTP & API → SMTP.",
    "resend": "Resend's SMTP user is the literal word 'resend'; the password is "
              "the API key.",
}

SEND_TIMEOUT = 30.0
# A letter undelivered for a day is stale; keeping it would land a week-old backlog
# at once when the mail server comes back.
MAX_AGE_SECONDS = 24 * 3600
# 30 s, 1 min, 2 min, 4 min ... capped. Not tighter: a mail server refusing
# connections must not be hammered by every station at once.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0


class MailError(Exception):
    """Anything that stopped a letter from being handed to the server."""


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_account(alerts_cfg=None):
    """``source`` says where the account was found, so nobody edits a file that another
    one overrides.
    """
    candidates = [(path, _read_json(path)) for path in ACCOUNT_FILES]
    candidates.append(("config.json alerts.smtp",
                       (alerts_cfg or {}).get("smtp")))
    for source, raw in candidates:
        if not raw or not raw.get("user"):
            continue
        preset = str(raw.get("preset") or "custom").lower()
        base = dict(SMTP_PRESETS.get(preset, {}))
        # The file wins over the preset: a provider moving a port needs no new release.
        account = {
            "preset": preset,
            "host": raw.get("host") or base.get("host", ""),
            "port": int(raw.get("port") or base.get("port", 0) or 0),
            "security": str(raw.get("security") or base.get("security", "ssl")),
            "user": raw.get("user", ""),
            "password": raw.get("password", ""),
            "from": raw.get("from") or raw.get("user", ""),
            "source": source,
        }
        if not account["host"] or not account["port"]:
            continue
        return account
    return None


def account_summary(account):
    if not account:
        return ("no sender configured — put mail_account.json in "
                f"{HOME_DIR} (see mail_account.json.example)")
    return f"{account['from']} via {account['host']}:{account['port']} " \
           f"(from {account['source']})"


def _connect(account, timeout):
    host, port = account["host"], int(account["port"])
    security = account.get("security", "ssl")
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout,
                                context=ssl.create_default_context())
    server = smtplib.SMTP(host, port, timeout=timeout)
    if security == "starttls":
        server.starttls(context=ssl.create_default_context())
    return server


def _explain(exc, account):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        hint = PRESET_HINTS.get(account.get("preset", ""), "")
        return f"the server rejected the login for {account['user']}. {hint}".strip()
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"the server refused every recipient: {exc.recipients}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"the server refused {account['from']} as a sender — with most "
                f"providers the From address must be the mailbox that logged in")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return (f"no answer from {account['host']}:{account['port']} within "
                f"{SEND_TIMEOUT:.0f} s — the port is most likely blocked outbound")
    if isinstance(exc, (OSError, ssl.SSLError)):
        return f"could not reach {account['host']}:{account['port']}: {exc}"
    return str(exc)


def send_now(account, to, subject, body, timeout=SEND_TIMEOUT):
    """Blocking, so nothing on a measurement path calls it. Raises :class:`MailError`.
    """
    if not account:
        raise MailError("no sender account configured")
    recipients = [addr for addr in (to or []) if addr]
    if not recipients:
        raise MailError("no recipients configured")

    message = EmailMessage()
    message["From"] = account["from"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)

    try:
        server = _connect(account, timeout)
    except Exception as exc:
        raise MailError(_explain(exc, account)) from exc
    try:
        if account.get("password"):
            server.login(account["user"], account["password"])
        server.send_message(message)
    except Exception as exc:
        raise MailError(_explain(exc, account)) from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass


class Quota:
    """A daily cap per machine.

    One mailbox serves the whole fleet, so a station stuck in a repeating failure would
    otherwise spend everyone's allowance before dawn, while everyone believes alerts
    work.
    """

    def __init__(self, path=QUOTA_FILE, limit=50):
        self.path = path
        self.limit = int(limit)

    def _state(self):
        data = _read_json(self.path) or {}
        today = dt.now().strftime("%Y-%m-%d")
        if data.get("date") != today:
            return {"date": today, "sent": 0, "announced": False}
        return {"date": today,
                "sent": int(data.get("sent") or 0),
                "announced": bool(data.get("announced"))}

    def _save(self, state):
        _write_json_atomic(self.path, state)

    def remaining(self):
        if self.limit <= 0:
            return 1 << 30        # a limit of zero means "no limit"
        return max(0, self.limit - self._state()["sent"])

    def count_one(self):
        state = self._state()
        state["sent"] += 1
        self._save(state)

    def take_announcement(self):
        """True exactly once per day, for the letter that says the cap is reached."""
        state = self._state()
        if state["announced"]:
            return False
        state["announced"] = True
        self._save(state)
        return True


def _write_json_atomic(path, data):
    """The reader is another process: a half-written letter it picks up is a letter
    lost.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Spool:
    """Writers only add; the sentinel drains."""

    def __init__(self, directory=SPOOL_DIR):
        self.dir = directory

    def put(self, subject, body, to=None, kind=""):
        """Never raises: a failure to warn must not become a crash.

        Returns None if the letter could not be queued; the message is in the log
        anyway.
        """
        letter = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "created": time.time(),
            "created_iso": dt.now().isoformat(timespec="seconds"),
            "to": list(to or []),
            "subject": subject,
            "body": body,
            "attempts": 0,
            "last_error": "",
            "next_attempt": 0.0,
        }
        name = f"{dt.now().strftime('%Y%m%d_%H%M%S')}_{letter['id']}.json"
        path = os.path.join(self.dir, name)
        try:
            _write_json_atomic(path, letter)
        except OSError:
            return None
        return path

    def pending(self):
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return []
        letters = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            data = _read_json(path)
            if data is None:
                # Not JSON and never will be: left in place it would fail on every pass,
                # for ever.
                _discard(path)
                continue
            data["_path"] = path
            letters.append(data)
        return letters

    def send_all(self, account, default_to, quota=None, now=None):
        """Never raises: a mail server's bad night must not take the sentinel down."""
        now = time.time() if now is None else now
        sent = failed = dropped = 0
        for letter in self.pending():
            path = letter["_path"]
            if now - float(letter.get("created") or 0) > MAX_AGE_SECONDS:
                _discard(path)
                dropped += 1
                continue
            if now < float(letter.get("next_attempt") or 0):
                continue
            if quota is not None and quota.remaining() <= 0:
                break
            recipients = letter.get("to") or list(default_to or [])
            try:
                send_now(account, recipients, letter.get("subject", ""),
                         letter.get("body", ""))
            except MailError as exc:
                letter["attempts"] = int(letter.get("attempts") or 0) + 1
                letter["last_error"] = str(exc)
                letter["next_attempt"] = now + min(
                    RETRY_MAX_SECONDS,
                    RETRY_BASE_SECONDS * (2 ** (letter["attempts"] - 1)))
                letter.pop("_path", None)
                try:
                    _write_json_atomic(path, letter)
                except OSError:
                    pass
                failed += 1
                continue
            _discard(path)
            if quota is not None:
                quota.count_one()
            sent += 1
        return sent, failed, dropped

    def count(self):
        try:
            return sum(1 for name in os.listdir(self.dir)
                       if name.endswith(".json"))
        except OSError:
            return 0


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass
