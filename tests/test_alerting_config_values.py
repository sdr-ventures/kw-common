"""`config_values`: a service passes its own settings, merged over the shared file's.

No openssl and no network. The send path is stopped at its connection constructor, and the
channels `validate_boot` announces through are replaced by recording stand-ins.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from kw_common import alerting, alerting_env
from kw_common.alerting import ERROR, AlertConfig, Alerter, AlertSettings
from kw_common.alerting_env import (
    AlertEnvError,
    _config_digest,
    _marker_body,
    _marker_matches,
    config_file_for,
    load_alert_settings,
    marker_name,
    validate_boot,
)
from test_alerting_env import drop, write_shared

DB_HOST = "db-smtp.example.com"
DB_VALUE = "not-a-real-db-password"
FILE_PROXY = "file-proxy.example.com:3128"
CALLER_PROXY = "caller-proxy.example.com:3128"
ENV_PROXY = "env-proxy.example.com:3128"


class Recorder:
    """A channel stand-in that records the titles it is asked to send."""

    def __init__(self) -> None:
        self.titles: list[str] = []

    def __call__(self, cfg: AlertConfig, spec: alerting.SeveritySpec, title: str,
                 message: str) -> None:
        self.titles.append(title)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(alerting, "_CHANNELS", (("email", recorder), ("ntfy", Recorder())))
    return recorder


def _settings(root: Path, values: dict[str, str] | None, **file: str) -> AlertSettings:
    write_shared(root, **file)
    return load_alert_settings(config_file_for(root), "dev", "feed-poller", config_values=values)


def test_the_callers_values_replace_the_files_key_by_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"SMTP_HOST": DB_HOST, "SMTP_PASSWORD": DB_VALUE})
    email = AlertConfig.load(settings).email
    assert email["SMTP_HOST"] == DB_HOST and email["SMTP_PASSWORD"] == DB_VALUE
    # Keys the caller did not pass still come from the file.
    assert email["EMAIL_TO"] == "ops@example.com" and email["SMTP_PORT"] == "587"


def test_a_key_the_caller_passes_blank_blanks_the_file_value_and_an_absent_key_does_not(
        tmp_path: Path) -> None:
    email = AlertConfig.load(_settings(tmp_path, {"EMAIL_TO": "  "})).email
    assert email["EMAIL_TO"] == ""
    assert email["EMAIL_FROM"] == "svc@example.com"


def test_no_values_is_exactly_the_file(tmp_path: Path) -> None:
    for passed in (None, {}):
        settings = _settings(tmp_path, passed)
        assert settings.config_values is None
        assert AlertConfig.load(settings).email["SMTP_HOST"] == "smtp.example.com"


def test_the_file_is_still_re_read_on_every_load_for_the_keys_the_caller_did_not_pass(
        tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"SMTP_HOST": DB_HOST})
    write_shared(tmp_path, EMAIL_TO="rotated@example.com", SMTP_HOST="ignored.example.com")
    email = AlertConfig.load(settings).email
    assert email["EMAIL_TO"] == "rotated@example.com"   # the file is live
    assert email["SMTP_HOST"] == DB_HOST                # the caller still wins


def test_the_send_path_connects_to_the_callers_host(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Reached(Exception):
        """Raised by the stand-in so the send stops before any socket is opened."""

    seen: list[tuple[object, ...]] = []

    def stand_in(*args: object, **kwargs: object) -> None:
        seen.append(args)
        raise Reached

    monkeypatch.setattr(alerting.smtplib, "SMTP", stand_in)
    cfg = AlertConfig.load(_settings(tmp_path, {"SMTP_HOST": DB_HOST, "SMTP_PORT": "2525"}))
    with pytest.raises(Reached):
        alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "t", "m")
    (args,) = seen
    assert args[:2] == (DB_HOST, 2525)


def test_the_callers_ntfy_keys_are_what_the_loader_resolves(tmp_path: Path) -> None:
    own = "https://ntfy.example.com/caller-topic"
    settings = _settings(tmp_path, {"NTFY_URL_FEED_POLLER": own})
    assert settings.ntfy_url == own and settings.title_prefix == ""   # its own topic: no prefix
    shared = "https://ntfy.example.com/caller-shared"
    settings = _settings(tmp_path, {"NTFY_URL_DEV": shared})
    assert settings.ntfy_url == shared and settings.title_prefix == "[dev][feed-poller] "


@pytest.mark.parametrize("env,caller,file,expected,source", [
    (ENV_PROXY, CALLER_PROXY, FILE_PROXY, ENV_PROXY, "environment"),
    (None, CALLER_PROXY, FILE_PROXY, CALLER_PROXY, "caller"),
    (None, "", FILE_PROXY, "", "caller"),          # the caller says: no proxy
    (None, None, FILE_PROXY, FILE_PROXY, "file"),
    (None, None, "", "", "unset"),
])
def test_the_proxy_precedence_and_its_boot_line(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, env: str | None, caller: str | None,
        file: str, expected: str, source: str) -> None:
    values = None if caller is None else {"SMTP_PROXY": caller}
    write_shared(tmp_path, SMTP_PROXY=file)
    with caplog.at_level(logging.INFO):
        settings = load_alert_settings(config_file_for(tmp_path), "dev", "feed-poller",
                                       smtp_proxy=env, config_values=values)
    assert AlertConfig.load(settings).email["SMTP_PROXY"] == expected
    assert f"SMTP_PROXY from {source} " in caplog.text
    for secret in (ENV_PROXY, CALLER_PROXY, FILE_PROXY):
        assert secret not in caplog.text


def test_a_value_is_never_in_a_repr_a_log_line_or_a_refusal(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        settings = _settings(tmp_path, {"SMTP_PASSWORD": DB_VALUE, "SMTP_HOSTT": "x"})
    assert DB_VALUE not in repr(settings) and DB_VALUE not in caplog.text
    assert "SMTP_PASSWORD" in caplog.text            # the KEY names are logged
    assert "SMTP_HOSTT" in caplog.text               # a typo is named, not silently ignored
    not_strings: dict[str, object] = {"SMTP_PASSWORD": DB_VALUE, "SMTP_PORT": 587}
    with pytest.raises(AlertEnvError) as bad:
        load_alert_settings(config_file_for(tmp_path), "dev", "feed-poller",
                            config_values=not_strings)  # type: ignore[arg-type]
    assert "SMTP_PORT" in str(bad.value) and DB_VALUE not in str(bad.value)


@pytest.mark.parametrize("values", ["SMTP_HOST=x", [("SMTP_HOST", "x")], {"": "x"}, {1: "x"}])
def test_a_malformed_mapping_is_refused_as_AlertEnvError(tmp_path: Path, values: object) -> None:
    write_shared(tmp_path)
    with pytest.raises(AlertEnvError):
        load_alert_settings(config_file_for(tmp_path), "dev", "feed-poller",
                            config_values=values)  # type: ignore[arg-type]


def test_alert_settings_refuses_a_non_string_value_without_echoing_a_value() -> None:
    not_strings: dict[str, object] = {"SMTP_PASSWORD": DB_VALUE, "SMTP_PORT": 587}
    with pytest.raises(ValueError, match="SMTP_PORT") as bad:
        AlertSettings(service="feed-poller",
                      config_values=not_strings)  # type: ignore[arg-type]
    assert DB_VALUE not in str(bad.value)
    with pytest.raises(ValueError, match="config_values"):
        AlertSettings(service="feed-poller", config_values="SMTP_HOST=x")  # type: ignore[arg-type]


def test_the_settings_keep_a_private_copy_and_stay_hashable() -> None:
    mine = {"SMTP_HOST": DB_HOST}
    settings = AlertSettings(service="feed-poller", config_values=mine)
    mine["SMTP_HOST"] = "changed.example.com"
    assert settings.config_values == {"SMTP_HOST": DB_HOST}
    assert isinstance(hash(settings), int)


def test_an_object_that_is_not_alert_settings_still_loads(tmp_path: Path) -> None:
    """An `Alerter` takes any settings-shaped object; one without the field changes nothing."""
    class Old:
        service = "feed-poller"
        config_file = str(write_shared(tmp_path))
        ntfy_url = ""

    assert AlertConfig.load(Old()).email["SMTP_HOST"] == "smtp.example.com"  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ validate_boot


def test_validate_boot_judges_the_merged_values(tmp_path: Path, sent: Recorder) -> None:
    shared, app = tmp_path / "shared", tmp_path / "app"
    drop(shared, "SMTP_PASSWORD")
    bare = load_alert_settings(config_file_for(shared), "dev", "feed-poller")
    with pytest.raises(AlertEnvError, match="SMTP_PASSWORD"):
        validate_boot(bare, "dev", app, alerter=Alerter(bare))
    full = load_alert_settings(config_file_for(shared), "dev", "feed-poller",
                               config_values={"SMTP_PASSWORD": DB_VALUE})
    assert validate_boot(full, "dev", app, alerter=Alerter(full)) is True
    assert (app / marker_name("dev")).is_file() and len(sent.titles) == 1


def test_a_change_to_the_callers_values_re_validates_and_an_unchanged_boot_skips(
        tmp_path: Path, sent: Recorder) -> None:
    shared, app = tmp_path / "shared", tmp_path / "app"
    write_shared(shared)

    def boot(values: dict[str, str] | None) -> bool:
        settings = load_alert_settings(config_file_for(shared), "dev", "feed-poller",
                                       config_values=values)
        return validate_boot(settings, "dev", app, alerter=Alerter(settings))

    assert boot({"SMTP_HOST": DB_HOST}) is True
    assert boot({"SMTP_HOST": DB_HOST}) is False                    # same file, same values
    assert boot({"SMTP_HOST": "other-db.example.com"}) is True      # a stored value changed
    assert boot(None) is True                                       # and back to the file alone


def test_the_marker_digest_of_a_service_with_no_values_is_the_files_alone(tmp_path: Path) -> None:
    """An upgrade must not re-validate (and re-announce) a service that passes nothing."""
    path = write_shared(tmp_path)
    want = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert _config_digest(path) == want == _config_digest(path, {}) == _config_digest(path, None)
    assert _config_digest(path, {"SMTP_HOST": DB_HOST}) != want
    marker = tmp_path / "marker"
    marker.write_text(_marker_body(want, "feed-poller"), encoding="utf-8")
    assert _marker_matches(marker, path, "feed-poller") is True
    assert _marker_matches(marker, path, "feed-poller", {"SMTP_HOST": DB_HOST}) is False


def test_values_only_builds_and_sends_with_no_file_read_anywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No file is passed, none is read on the build or the send, and the send reaches the host."""
    class Reached(Exception):
        """Raised by the stand-in so the send stops before any socket is opened."""

    seen: list[tuple[object, ...]] = []

    def stand_in(*args: object, **kwargs: object) -> None:
        seen.append(args)
        raise Reached

    def no_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a file was read")

    monkeypatch.setattr(alerting.smtplib, "SMTP", stand_in)
    monkeypatch.setattr(alerting_env, "read_config", no_read)
    monkeypatch.setattr(alerting, "parse_env_file", no_read)
    settings = load_alert_settings(None, "dev", "feed-poller",
                                   config_values={"SMTP_HOST": DB_HOST, "SMTP_PORT": "2525",
                                                  "EMAIL_FROM": "svc@example.com",
                                                  "EMAIL_TO": "ops@example.com"})
    assert settings.config_file is None
    cfg = AlertConfig.load(settings)
    with pytest.raises(Reached):
        alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "t", "m")
    (args,) = seen
    assert args[:2] == (DB_HOST, 2525)
