"""Alert mail: the queue on disk, the daily cap, and where the sender comes from.

What these tests exist for: the alert system's whole promise is that a fault
which kills the program still reaches somebody. That promise rests on one
mechanism — the letter is a file on disk before anything touches the network,
written by the worker and sent by another process — so the tests below are
mostly about what survives, what is retried, and what is thrown away.

Nothing here opens a socket. ``smtplib`` is replaced wherever a test needs a
server, so a machine with no network, or with a real mailbox configured in the
developer's home directory, gets the same answers.
"""
import json
import sys
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mailer                                                    # noqa: E402
from mailer import MailError, Quota, Spool                       # noqa: E402


ACCOUNT = {
    "preset": "yandex", "host": "smtp.example.org", "port": 465,
    "security": "ssl", "user": "obs@example.org", "password": "x",
    "from": "obs@example.org", "source": "test",
}


@pytest.fixture
def spool(tmp_path):
    return Spool(str(tmp_path / "outbox"))


@pytest.fixture
def refuse(monkeypatch):
    """Make every send fail, and count the attempts."""
    calls = []

    def fake(account, to, subject, body, timeout=None):
        calls.append(subject)
        raise MailError("server said no")

    monkeypatch.setattr(mailer, "send_now", fake)
    return calls


@pytest.fixture
def accept(monkeypatch):
    """Make every send succeed, and record what was handed over."""
    calls = []

    def fake(account, to, subject, body, timeout=None):
        calls.append({"to": list(to), "subject": subject, "body": body})

    monkeypatch.setattr(mailer, "send_now", fake)
    return calls


# -- the queue survives the process ------------------------------------------
def test_a_letter_queued_before_a_crash_is_still_there_afterwards(spool):
    # The worker writes and dies. "Another process" is, to the disk, simply
    # another Spool object opened on the same directory.
    spool.put("camera stopped", "traceback...", to=["a@example.org"], kind="crash")
    assert len(Spool(spool.dir).pending()) == 1


def test_queueing_a_letter_touches_no_network(spool, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("put() must never send anything itself")

    monkeypatch.setattr(mailer, "send_now", explode)
    assert spool.put("subject", "body") is not None


def test_a_letter_that_cannot_be_written_is_not_an_exception(monkeypatch, spool):
    # A station whose home directory is full must lose the warning, not the night.
    monkeypatch.setattr(mailer, "_write_json_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert spool.put("subject", "body") is None


def test_a_delivered_letter_leaves_the_queue(spool, accept):
    spool.put("subject", "body", to=["a@example.org"])
    assert spool.send_all(ACCOUNT, []) == (1, 0, 0)
    assert spool.pending() == []
    assert accept[0]["to"] == ["a@example.org"]


def test_a_letter_without_its_own_recipients_uses_the_configured_ones(spool, accept):
    spool.put("subject", "body")
    spool.send_all(ACCOUNT, ["team@example.org"])
    assert accept[0]["to"] == ["team@example.org"]


# -- what happens when the server will not take it ---------------------------
def test_a_refused_letter_is_kept_rather_than_lost(spool, refuse):
    spool.put("subject", "body", to=["a@example.org"])
    assert spool.send_all(ACCOUNT, []) == (0, 1, 0)
    still_there = spool.pending()
    assert len(still_there) == 1
    assert still_there[0]["attempts"] == 1
    assert "server said no" in still_there[0]["last_error"]


def test_retries_back_off_instead_of_hammering(spool, refuse):
    spool.put("subject", "body", to=["a@example.org"])
    now = time.time()
    spool.send_all(ACCOUNT, [], now=now)
    # The second pass a moment later must not even try: every station on site
    # retrying a dead server every fifteen seconds is its own outage.
    spool.send_all(ACCOUNT, [], now=now + 1)
    assert len(refuse) == 1

    spool.send_all(ACCOUNT, [], now=now + mailer.RETRY_BASE_SECONDS + 1)
    assert len(refuse) == 2


def test_a_letter_older_than_a_day_is_given_up_on(spool, refuse):
    path = spool.put("subject", "body", to=["a@example.org"])
    letter = json.loads(Path(path).read_text(encoding="utf-8"))
    letter["created"] = time.time() - mailer.MAX_AGE_SECONDS - 1
    Path(path).write_text(json.dumps(letter), encoding="utf-8")

    assert spool.send_all(ACCOUNT, []) == (0, 0, 1)
    assert spool.pending() == []
    assert refuse == []


def test_a_file_that_is_not_a_letter_is_discarded_not_retried_forever(spool):
    Path(spool.dir).mkdir(parents=True, exist_ok=True)
    (Path(spool.dir) / "20260826_000000_junk.json").write_text("{ not json",
                                                               encoding="utf-8")
    assert spool.pending() == []
    assert spool.count() == 0


# -- the daily cap -----------------------------------------------------------
def test_the_daily_cap_stops_one_station_spending_the_whole_allowance(
        spool, accept, tmp_path):
    quota = Quota(str(tmp_path / "quota.json"), limit=2)
    for i in range(5):
        spool.put(f"subject {i}", "body", to=["a@example.org"])

    sent, _, _ = spool.send_all(ACCOUNT, [], quota=quota)
    assert sent == 2
    assert quota.remaining() == 0
    # The rest are still queued rather than thrown away: tomorrow's allowance
    # will carry them, and until then the log has everything.
    assert len(spool.pending()) == 3


def test_a_cap_of_zero_means_no_cap(spool, accept, tmp_path):
    quota = Quota(str(tmp_path / "quota.json"), limit=0)
    for i in range(4):
        spool.put(f"subject {i}", "body", to=["a@example.org"])
    sent, _, _ = spool.send_all(ACCOUNT, [], quota=quota)
    assert sent == 4


def test_the_cap_is_announced_once_and_not_every_pass(tmp_path):
    quota = Quota(str(tmp_path / "quota.json"), limit=1)
    assert quota.take_announcement() is True
    assert quota.take_announcement() is False


# -- where the sender comes from ---------------------------------------------
@pytest.fixture
def account_files(tmp_path, monkeypatch):
    """Point the three lookup places at a temporary directory."""
    home = tmp_path / "home.json"
    checkout = tmp_path / "checkout.json"
    monkeypatch.setattr(mailer, "ACCOUNT_FILES", (str(home), str(checkout)))
    return home, checkout


def _write(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_a_preset_supplies_host_and_port_so_the_file_need_not(account_files):
    home, _ = account_files
    _write(home, preset="yandex", user="obs@yandex.ru", password="x")
    account = mailer.resolve_account()
    assert account["host"] == "smtp.yandex.ru"
    assert account["port"] == 465
    assert account["security"] == "ssl"
    # An account file that did not say who it is from still has a From address.
    assert account["from"] == "obs@yandex.ru"


def test_the_home_directory_wins_over_the_checkout(account_files):
    home, checkout = account_files
    _write(home, preset="yandex", user="home@example.org", password="x")
    _write(checkout, preset="gmail", user="checkout@example.org", password="x")
    assert mailer.resolve_account()["user"] == "home@example.org"


def test_the_checkout_is_used_when_the_home_directory_has_nothing(account_files):
    _, checkout = account_files
    _write(checkout, preset="gmail", user="checkout@example.org", password="x")
    account = mailer.resolve_account()
    assert account["user"] == "checkout@example.org"
    assert account["security"] == "starttls"


def test_a_station_can_override_both_from_its_own_config(account_files):
    account = mailer.resolve_account(
        {"smtp": {"preset": "mailru", "user": "site@mail.ru", "password": "x"}})
    assert account["user"] == "site@mail.ru"
    assert "config.json" in account["source"]


def test_a_spelled_out_host_overrides_the_preset(account_files):
    home, _ = account_files
    _write(home, preset="yandex", host="smtp.internal", port=2525,
           security="starttls", user="obs@example.org", password="x")
    account = mailer.resolve_account()
    assert (account["host"], account["port"]) == ("smtp.internal", 2525)


def test_a_file_with_no_login_is_not_an_account(account_files):
    home, _ = account_files
    _write(home, preset="yandex", password="x")
    assert mailer.resolve_account() is None


def test_no_account_anywhere_is_reported_rather_than_guessed(account_files):
    assert mailer.resolve_account() is None
    assert "mail_account.json" in mailer.account_summary(None)


# -- what the test button will say -------------------------------------------
class _RejectingServer:
    def __init__(self, *args, **kwargs):
        pass

    def login(self, user, password):
        import smtplib
        raise smtplib.SMTPAuthenticationError(535, b"bad login")

    def send_message(self, message):
        raise AssertionError("should not get this far")

    def quit(self):
        pass


def test_a_rejected_login_says_so_and_names_the_password_to_use(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _RejectingServer)
    with pytest.raises(MailError) as caught:
        mailer.send_now(ACCOUNT, ["a@example.org"], "subject", "body")
    message = str(caught.value)
    assert "obs@example.org" in message
    # And the fix, not just the fact: this is the mistake everyone makes first.
    assert "application password" in message


def test_sending_with_no_recipients_is_refused_before_connecting(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL",
                        lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(MailError):
        mailer.send_now(ACCOUNT, [], "subject", "body")
    with pytest.raises(MailError):
        mailer.send_now(None, ["a@example.org"], "subject", "body")
