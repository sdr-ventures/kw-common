"""`SMTP_PROXY` set by the environment overrides the shared file's value for one service.

Needs no openssl and no network: the send path is stopped at its connection constructor, and the
assertions are on which constructor ran and with what proxy.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from kw_common import alerting
from kw_common.alerting import ERROR, AlertConfig, AlertSettings
from kw_common.alerting_env import (
    DEPLOY_ENV_VAR,
    SHARED_ROOT_VAR,
    config_file_for,
    load_alert_settings,
    load_alert_settings_from_env,
)
from test_alerting_env import write_shared

FILE_PROXY = "file-proxy.example.com:3128"
ENV_PROXY = "env-proxy.example.com:3128"


@pytest.fixture
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(SHARED_ROOT_VAR, str(tmp_path))
    monkeypatch.setenv(DEPLOY_ENV_VAR, "dev")
    monkeypatch.delenv("SMTP_PROXY", raising=False)
    return tmp_path


def _env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("SMTP_PROXY", raising=False)
    else:
        monkeypatch.setenv("SMTP_PROXY", value)


def _loaded(shared: Path, file_proxy: str) -> AlertConfig:
    write_shared(shared, SMTP_PROXY=file_proxy)
    return AlertConfig.load(load_alert_settings_from_env("feed-poller"))


@pytest.mark.parametrize("env,expected", [
    (ENV_PROXY, ENV_PROXY),   # set: replaces the file's value
    ("", ""),                 # set but blank: no proxy, even though the file names one
    (None, FILE_PROXY),       # unset: the file decides, exactly as before
])
def test_the_environment_takes_precedence_over_the_file(
        shared: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, expected: str) -> None:
    _env(monkeypatch, env)
    assert _loaded(shared, FILE_PROXY).email["SMTP_PROXY"] == expected


def test_no_environment_value_and_no_file_value_is_no_proxy(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, None)
    assert _loaded(shared, "").email["SMTP_PROXY"] == ""


@pytest.mark.parametrize("env,expected_proxy", [
    (ENV_PROXY, ENV_PROXY),   # the send path tunnels through the environment's proxy
    ("", None),               # blank: the send path connects directly
    (None, FILE_PROXY),       # unset: the send path tunnels through the file's proxy
])
def test_the_send_path_follows_the_precedence(
        shared: Path, monkeypatch: pytest.MonkeyPatch, env: str | None,
        expected_proxy: str | None) -> None:
    class Reached(Exception):
        """Raised by the stand-in constructors so the send stops before any socket is opened."""

    calls: list[tuple[str, tuple[object, ...]]] = []

    def stand_in(name: str) -> Callable[..., None]:
        def ctor(*args: object, **kwargs: object) -> None:
            calls.append((name, args))
            raise Reached
        return ctor

    monkeypatch.setattr(alerting, "_TunnelSMTP", stand_in("tunnel"))
    monkeypatch.setattr(alerting.smtplib, "SMTP", stand_in("direct"))
    _env(monkeypatch, env)
    cfg = _loaded(shared, FILE_PROXY)

    with pytest.raises(Reached):
        alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "t", "m")

    (name, args), = calls
    if expected_proxy is None:
        assert name == "direct"
    else:
        assert name == "tunnel"
        assert args[0] == expected_proxy


def test_a_malformed_environment_proxy_is_a_setting_fault_and_is_never_echoed(
        shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    userinfo = "not-a-real-userinfo"
    _env(monkeypatch, f"http://user:{userinfo}@env-proxy.example.com:3128/x")
    faults = _loaded(shared, FILE_PROXY).setting_faults()
    assert faults, "a malformed environment value must be reported like a malformed file value"
    assert userinfo not in " ".join(faults)


@pytest.mark.parametrize("env,file_proxy,source", [
    (ENV_PROXY, FILE_PROXY, "environment"),
    ("", FILE_PROXY, "environment"),
    (None, FILE_PROXY, "file"),
    (None, "", "unset"),
])
def test_the_boot_line_names_the_source_and_never_the_value(
        shared: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        env: str | None, file_proxy: str, source: str) -> None:
    write_shared(shared, SMTP_PROXY=file_proxy)
    _env(monkeypatch, env)
    with caplog.at_level(logging.INFO):
        load_alert_settings_from_env("feed-poller")
    assert f"SMTP_PROXY from {source} " in caplog.text
    assert ENV_PROXY not in caplog.text
    assert FILE_PROXY not in caplog.text


def test_the_loader_passes_the_value_through_and_defaults_to_none(tmp_path: Path) -> None:
    write_shared(tmp_path, SMTP_PROXY=FILE_PROXY)
    path = config_file_for(tmp_path)
    assert load_alert_settings(path, "dev", "feed-poller").smtp_proxy is None
    assert load_alert_settings(path, "dev", "feed-poller", smtp_proxy="").smtp_proxy == ""
    assert load_alert_settings(path, "dev", "feed-poller",
                               smtp_proxy=ENV_PROXY).smtp_proxy == ENV_PROXY


def test_a_non_string_override_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="smtp_proxy"):
        AlertSettings(service="feed-poller", smtp_proxy=8080)  # type: ignore[arg-type]
