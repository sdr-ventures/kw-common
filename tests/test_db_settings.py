"""Values-only services: boot validation and a one-recipient send, with no shared file.

A service that keeps its alert settings in its own database builds them with
`load_alert_settings(None, ..., config_values=...)`. These tests pin the two things that path
needs: `validate_boot` judges those values alone (no file is read, no layout is checked), and
`send_email` sends one message through the same channel an alert uses.

Nothing here reaches the network: the SMTP client is a recording stand-in, and the alert channel
is replaced by a recorder, so the autouse block in conftest is never tripped.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from kw_common import alerting, alerting_env
from kw_common.alerting import (
    ERROR,
    OK,
    Alerter,
    AlertSettings,
    EmailUnavailable,
    alerter_scope,
    send_email,
)
from kw_common.alerting_env import (
    AlertEnvError,
    load_alert_settings,
    marker_name,
    validate_boot,
)

GOOD = {
    "NTFY_URL_DEV": "https://ntfy.example.com/alerts-dev",
    "EMAIL_TO": "ops@example.com",
    "EMAIL_FROM": "alerts@example.com",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_PASSWORD": "app-password-123",
}
SECRET = GOOD["SMTP_PASSWORD"]


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The alert channel is a recorder: the titles that would have gone out, nothing more."""
    titles: list[str] = []

    def channel(cfg: object, spec: object, title: str, message: str) -> None:
        titles.append(title)

    monkeypatch.setattr(alerting, "_CHANNELS", (("email", channel),))
    return titles


def _values_only(values: dict[str, str] | None) -> AlertSettings:
    return load_alert_settings(None, "dev", "feed-poller", config_values=values)


def test_values_only_validate_boot_passes_and_marks_the_boot(tmp_path: Path,
                                                             delivered: list[str]) -> None:
    settings = _values_only(GOOD)
    assert validate_boot(settings, "dev", tmp_path, alerter=Alerter(settings)) is True
    assert (tmp_path / marker_name("dev")).is_file()
    assert len(delivered) == 1


def test_values_only_second_identical_boot_skips_and_a_changed_value_re_validates(
        tmp_path: Path, delivered: list[str]) -> None:
    def boot(values: dict[str, str]) -> bool:
        settings = _values_only(values)
        return validate_boot(settings, "dev", tmp_path, alerter=Alerter(settings))

    assert boot(GOOD) is True
    assert boot(GOOD) is False
    assert boot({**GOOD, "SMTP_HOST": "smtp2.example.com"}) is True
    assert len(delivered) == 2


def test_values_only_validation_reads_no_file_and_checks_no_layout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivered: list[str]) -> None:
    def no_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a file was read for a values-only service")

    monkeypatch.setattr(alerting_env, "read_config", no_read)
    monkeypatch.setattr(alerting_env, "_check_layout",
                        lambda config, root: _assert_no_file(config))
    settings = _values_only(GOOD)
    assert validate_boot(settings, "dev", tmp_path, alerter=Alerter(settings)) is True


def _assert_no_file(config: object) -> None:
    assert config is None, "a values-only service was given a config path to check"


def test_values_only_with_neither_file_nor_values_refuses(tmp_path: Path) -> None:
    with pytest.raises(AlertEnvError, match="neither a config_file nor config_values"):
        validate_boot(_values_only(None), "dev", tmp_path)


def test_values_only_refusal_names_the_source_not_a_value(tmp_path: Path,
                                                          delivered: list[str]) -> None:
    missing = {k: v for k, v in GOOD.items() if k != "SMTP_PASSWORD"}
    settings = _values_only(missing)
    with pytest.raises(AlertEnvError) as info:
        validate_boot(settings, "dev", tmp_path, alerter=Alerter(settings))
    assert "the service's own settings" in str(info.value)
    assert "SMTP_PASSWORD" in str(info.value)
    assert SECRET not in str(info.value)
    assert delivered == []   # a failure never notifies
    assert not (tmp_path / marker_name("dev")).exists()


def test_values_only_placeholder_is_refused_by_key_name_only(tmp_path: Path,
                                                             delivered: list[str]) -> None:
    settings = _values_only({**GOOD, "SMTP_PASSWORD": "CHANGE-ME"})
    with pytest.raises(AlertEnvError) as info:
        validate_boot(settings, "dev", tmp_path, alerter=Alerter(settings))
    assert "placeholder for: SMTP_PASSWORD" in str(info.value)
    assert delivered == []


def test_values_only_digest_changes_with_the_values_and_not_otherwise(tmp_path: Path) -> None:
    base = alerting_env._config_digest(None, GOOD)
    assert base.startswith("sha256:")
    assert alerting_env._config_digest(None, dict(GOOD)) == base
    assert alerting_env._config_digest(None, {**GOOD, "SMTP_HOST": "x.example.com"}) != base


class _RecordingSMTP:
    """Stands in for `smtplib.SMTP`: records the conversation, sends nothing."""

    log: ClassVar[list[tuple[object, ...]]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.log.append(("connect", host, port))

    def __enter__(self) -> _RecordingSMTP:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, context: object = None) -> None:
        self.log.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user))

    def send_message(self, msg: object) -> dict[str, tuple[int, bytes]]:
        self.log.append(("send", msg["To"], msg["Subject"], msg.get_content()))  # type: ignore[index]
        return {}


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    _RecordingSMTP.log = []
    monkeypatch.setattr(alerting.smtplib, "SMTP", _RecordingSMTP)
    return _RecordingSMTP.log


def test_send_email_sends_one_message_with_the_subject_exactly_as_given(
        smtp: list[tuple[object, ...]]) -> None:
    settings = _values_only(GOOD)
    send_email("member@example.com", "Your invite", "Open the link.", alerter=Alerter(settings))
    assert ("connect", "smtp.example.com", 587) in smtp
    assert ("login", "alerts@example.com") in smtp
    (sent,) = [entry for entry in smtp if entry[0] == "send"]
    assert sent[1] == "member@example.com"     # one recipient, normalised
    assert sent[2] == "Your invite"             # no severity prefix added
    assert "Open the link." in sent[3]


def test_send_email_never_logs_the_subject_or_body(smtp: list[tuple[object, ...]],
                                                   caplog: pytest.LogCaptureFixture) -> None:
    settings = _values_only(GOOD)
    caplog.set_level(logging.DEBUG)
    send_email("member@example.com", "Invite token abc123", "https://x.example.com/t/abc123",
               alerter=Alerter(settings))
    assert "abc123" not in caplog.text


def test_send_email_needs_an_installed_alerter_or_refuses(smtp: list[tuple[object, ...]]) -> None:
    with alerter_scope(None), pytest.raises(EmailUnavailable, match="configure"):
        send_email("member@example.com", "s", "b")
    assert smtp == []


def test_send_email_refuses_an_unusable_channel(smtp: list[tuple[object, ...]]) -> None:
    settings = _values_only({**GOOD, "SMTP_PASSWORD": ""})
    with pytest.raises(EmailUnavailable):
        send_email("member@example.com", "s", "b", alerter=Alerter(settings))
    assert smtp == []


@pytest.mark.parametrize("to", ["a@example.com, b@example.com", "", "a@example.com;b@example.com"])
def test_send_email_takes_exactly_one_recipient(to: str, smtp: list[tuple[object, ...]]) -> None:
    settings = _values_only(GOOD)
    with pytest.raises(ValueError, match="exactly one recipient"):
        send_email(to, "s", "b", alerter=Alerter(settings))
    assert smtp == []


@pytest.mark.parametrize(("to", "subject"), [
    ("member@example.com", "line one\r\nBcc: someone@example.com"),
    ("member@example.com\r\nBcc: x@example.com", "s"),
])
def test_send_email_refuses_a_control_character_in_a_header(
        to: str, subject: str, smtp: list[tuple[object, ...]]) -> None:
    settings = _values_only(GOOD)
    with pytest.raises(ValueError, match="control"):
        send_email(to, subject, "b", alerter=Alerter(settings))
    assert smtp == []


def test_send_email_goes_through_the_alert_send_path_with_the_proxy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """One SMTP path for alerts and single sends: the proxy and the checks are not repeated here."""
    seen: list[tuple[str, str]] = []

    def spy(cfg: alerting.AlertConfig, subject: str, message: str) -> None:
        seen.append((subject, str((cfg.email or {}).get("SMTP_PROXY"))))

    monkeypatch.setattr(alerting, "_deliver_email", spy)
    settings = replace(_values_only(GOOD), smtp_proxy="proxy.example.com:3128")
    send_email("member@example.com", "Hello", "b", alerter=Alerter(settings))
    assert seen == [("Hello", "proxy.example.com:3128")]


def test_an_alert_still_prefixes_its_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The alert send keeps its severity prefix: `_send_email` now delegates, it did not change."""
    seen: list[str] = []
    monkeypatch.setattr(alerting, "_deliver_email",
                        lambda cfg, subject, message: seen.append(subject))
    cfg = alerting.AlertConfig.load(_values_only(GOOD))
    alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "svc: down", "m")
    alerting._send_email(cfg, alerting.SEVERITIES[OK], "svc: up", "m")
    assert seen == [f"{alerting.SEVERITIES[ERROR].prefix} svc: down",
                    f"{alerting.SEVERITIES[OK].prefix} svc: up"]
