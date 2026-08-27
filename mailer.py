"""Outgoing mail for the alert system — stdlib only, and never on a hot path.

Two things live here: the account (where the credentials come from and what a
provider's SMTP looks like) and the spool (a queue of letters on disk).

The spool is the whole design, not a convenience. A measurement thread must
never open a socket: an SMTP server that has gone slow would hold up a frame,
and one that hangs would hold up the night. So a worker only ever writes a JSON
file into ``~/.every_camera/outbox`` — microseconds, no network — and something
else sends it. That something else is ``sentinel.py``, which is a separate
process, which is what makes the promise this module exists for: **a letter
queued a second before the program dies still goes out.** A crash cannot
un-write a file.

The account lives outside ``config.json`` on purpose. The point of the whole
arrangement is that setting a station up means typing the addresses to notify
and nothing else, so the sender's credentials arrive with the installation the
way ``env.sh`` already does — one file, once, per machine. It is looked for in
three places, in this order:

    ~/.every_camera/mail_account.json   the normal place. Outside the checkout,
                                        so neither a commit nor ``update.py
                                        --force`` can touch it.
    <checkout>/mail_account.json        beside the program, like ``env.sh``.
    config.json ``alerts.smtp``         for a station that needs its own mailbox.

A preset supplies host, port and encryption, so the file holds a provider name,
a login and a password and nothing else:

    {"preset": "yandex", "user": "obs-alerts@yandex.ru",
     "password": "...", "from": "obs-alerts@yandex.ru"}
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

# What each provider's SMTP looks like, so that the account file does not have
# to. "ssl" connects encrypted; "starttls" connects in the clear and upgrades;
# "none" is for a relay on localhost that wants neither.
SMTP_PRESETS = {
    "yandex": {"host": "smtp.yandex.ru", "port": 465, "security": "ssl"},
    "mailru": {"host": "smtp.mail.ru", "port": 465, "security": "ssl"},
    "gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
    "brevo": {"host": "smtp-relay.brevo.com", "port": 587, "security": "starttls"},
    "resend": {"host": "smtp.resend.com", "port": 465, "security": "ssl"},
    "localhost": {"host": "localhost", "port": 25, "security": "none"},
    "custom": {},
}

# Yandex and Mail.ru want an application password rather than the account's own,
# and say so in a way worth repeating verbatim to whoever is setting this up.
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
# A letter nobody could deliver in a day is describing a fault that is either
# fixed or long since reported by other means; keeping it forever only means
# a week-old backlog lands at once when the mail server comes back.
MAX_AGE_SECONDS = 24 * 3600
# Retry spacing: 30 s, 1 min, 2 min, 4 min … capped. Deliberately not tighter —
# nothing here is urgent to the minute, and a mail server that is refusing
# connections should not be hammered by every station at once.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0


class MailError(Exception):
    """Anything that stopped a letter from being handed to the server."""


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_account(alerts_cfg=None):
    """The sender's credentials, or None if this machine has none.

    Returns a dict with ``host``, ``port``, ``security``, ``user``, ``password``,
    ``from`` and ``source`` — the last being where it was found, so the setup
    wizard and the log can say which of the three places is in play instead of
    leaving someone editing a file that is being overridden by another.
    """
    candidates = [(path, _read_json(path)) for path in ACCOUNT_FILES]
    candidates.append(("config.json alerts.smtp",
                       (alerts_cfg or {}).get("smtp")))
    for source, raw in candidates:
        if not raw or not raw.get("user"):
            continue
        preset = str(raw.get("preset") or "custom").lower()
        base = dict(SMTP_PRESETS.get(preset, {}))
        # Anything spelled out in the file wins over the preset: a provider that
        # moves a port should not need a new release to keep working.
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
    """One line for the wizard and the log: who sends, and from where."""
    if not account:
        return ("no sender configured — put mail_account.json in "
                f"{HOME_DIR} (see mail_account.json.example)")
    return f"{account['from']} via {account['host']}:{account['port']} " \
           f"(from {account['source']})"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
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
    """Turn an smtplib failure into something a person can act on.

    "Failed to send" is the least useful thing a test button can say. Almost
    every failure here is one of four things, and each has a different fix.
    """
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
    """Hand one letter to the server. Raises :class:`MailError` if it will not take it.

    Synchronous and blocking, which is why nothing on a measurement path calls
    it: the spool exists so that workers do not have to.
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


# ---------------------------------------------------------------------------
# Daily quota
# ---------------------------------------------------------------------------
class Quota:
    """A cap on letters per day, per machine.

    One mailbox serves the whole fleet, so the provider's daily limit is shared.
    Without a cap here, a single station stuck in a failure that repeats every
    frame would spend the allowance for every other station before dawn — which
    is the one way an alert system can fail that leaves everyone believing it
    still works. When the cap is hit the station says so once and goes quiet;
    the log keeps everything either way.
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


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------
def _write_json_atomic(path, data):
    """Write via a temporary file, as ``utils.save_config`` does.

    A half-written letter that a reader picks up mid-write is a letter lost, and
    the reader here is another process entirely.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Spool:
    """Letters waiting on disk. Writers only append; the sentinel drains."""

    def __init__(self, directory=SPOOL_DIR):
        self.dir = directory

    # -- the writing side (workers) -----------------------------------------
    def put(self, subject, body, to=None, kind=""):
        """Queue one letter. Never raises: a failure to warn must not become a crash.

        Returns the file path, or None if even this could not be done — in which
        case the caller has already written the real message to the log, which
        is the point of that log.
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

    # -- the sending side (sentinel) ----------------------------------------
    def pending(self):
        """Queued letters, oldest first. Unreadable files are dropped, not retried."""
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
                # Not JSON and never will be. Left in place it would be reported
                # as a failure on every pass, for ever.
                _discard(path)
                continue
            data["_path"] = path
            letters.append(data)
        return letters

    def send_all(self, account, default_to, quota=None, now=None):
        """Try to deliver everything due. Returns ``(sent, failed, dropped)``.

        Nothing in here raises: it runs in the sentinel's loop, and a mail server
        having a bad night must not take the watchdog down with it.
        """
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
