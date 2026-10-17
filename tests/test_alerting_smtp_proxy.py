"""Email through an HTTP CONNECT tunnel (`SMTP_PROXY`): a fake proxy relays to a fake SMTP server.

Everything is loopback. The server's certificate is for `smtp.example.com` and is trusted only via a
patched `ssl.create_default_context`, so host-name verification stays ON: the tunnel is addressed
to loopback but the handshake must still succeed against the mail host name.
"""
from __future__ import annotations

import logging
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from kw_common import alerting
from kw_common.alerting import ERROR, AlertConfig, AlertSettings
from test_alerting import FakeSMTP, write_email_config

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

MAIL_HOST = "smtp.example.com"


@pytest.fixture
def settings(tmp_path: Path) -> AlertSettings:
    return AlertSettings(service="svc", config_file=str(tmp_path / "alerting.env"),
                         ntfy_url="https://ntfy.example.com/svc",
                         state_file=str(tmp_path / "state.json"),
                         error_log=str(tmp_path / "logs" / "svc-errors.log"))


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={MAIL_HOST}",
         "-addext", f"subjectAltName=DNS:{MAIL_HOST}"],
        check=True, capture_output=True)
    return cert, key


@pytest.fixture(autouse=True)
def _trust_test_cert(monkeypatch: pytest.MonkeyPatch, tls_files: tuple[Path, Path]) -> None:
    real = ssl.create_default_context

    def ctx(*a: object, **kw: object) -> ssl.SSLContext:
        c = real(*a, **kw)
        c.load_verify_locations(str(tls_files[0]))
        return c

    monkeypatch.setattr(alerting.ssl, "create_default_context", ctx)


class FakeMailServer:
    """One-connection SMTP server. `implicit`: TLS at accept (465), else STARTTLS (587)."""

    def __init__(self, tls_files: tuple[Path, Path], implicit: bool) -> None:
        self.implicit, self.received = implicit, []
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(tls_files[0]), str(tls_files[1]))

    def serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            if self.implicit:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            f = conn.makefile("rwb", buffering=0)
            f.write(b"220 fake ESMTP\r\n")
            data = False
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.strip().upper()
                if data:
                    if line == b".\r\n":
                        data = False
                        f.write(b"250 queued\r\n")
                    else:
                        self.received.append(line)
                elif cmd.startswith(b"EHLO"):
                    f.write(b"250-fake\r\n" + (b"" if self.implicit else b"250-STARTTLS\r\n")
                            + b"250 AUTH PLAIN\r\n")
                elif cmd == b"STARTTLS":
                    f.write(b"220 go\r\n")
                    conn = self.ctx.wrap_socket(conn, server_side=True)
                    f = conn.makefile("rwb", buffering=0)
                elif cmd.startswith(b"AUTH"):
                    f.write(b"235 ok\r\n")
                elif cmd.startswith((b"MAIL", b"RCPT")):
                    f.write(b"250 ok\r\n")
                elif cmd == b"DATA":
                    data = True
                    f.write(b"354 go\r\n")
                elif cmd == b"QUIT":
                    f.write(b"221 bye\r\n")
                    return
                else:
                    f.write(b"250 ok\r\n")
        except OSError:
            pass
        finally:
            conn.close()


class FakeProxy:
    """Listens on loopback. `status` 200 relays CONNECT to `target`; anything else refuses."""

    def __init__(self, status: int = 200, target: FakeMailServer | None = None) -> None:
        self.status, self.target = status, target
        self.requests: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:  # closed by the fixture before anything connected
            return
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += conn.recv(1)
        self.requests.append(head.split(b"\r\n")[0].decode())
        if self.status != 200 or self.target is None:
            conn.sendall(f"HTTP/1.1 {self.status} Nope\r\n\r\n".encode())
            conn.close()
            return
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.target.serve(conn)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def make_proxy() -> Iterator[type[FakeProxy]]:
    made: list[FakeProxy] = []

    class P(FakeProxy):
        def __init__(self, *a: object, **kw: object) -> None:
            super().__init__(*a, **kw)  # type: ignore[arg-type]
            made.append(self)

    yield P
    for p in made:
        p.close()


def send(settings: AlertSettings, **cfg: str) -> None:
    write_email_config(settings, SMTP_HOST=MAIL_HOST, **cfg)
    cfg = AlertConfig.load(settings)
    alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "svc: down", "body-text")


@pytest.mark.parametrize("port,implicit", [("587", False), ("465", True)])
def test_email_goes_through_the_connect_tunnel_on_both_ports(
        settings: AlertSettings, tls_files: tuple[Path, Path], make_proxy: type[FakeProxy],
        port: str, implicit: bool) -> None:
    server = FakeMailServer(tls_files, implicit)
    proxy = make_proxy(200, server)
    send(settings, SMTP_PORT=port, SMTP_PROXY=proxy.addr)
    proxy.thread.join(10)
    assert proxy.requests == [f"CONNECT {MAIL_HOST}:{port} HTTP/1.1"]
    assert b"body-text\r\n" in server.received


def test_a_refused_connect_is_a_channel_failure_that_logs_and_does_not_raise(
        settings: AlertSettings, make_proxy: type[FakeProxy],
        caplog: pytest.LogCaptureFixture) -> None:
    proxy = make_proxy(403)
    write_email_config(settings, SMTP_HOST=MAIL_HOST, SMTP_PROXY=proxy.addr)
    alerter = alerting.Alerter(settings)
    with caplog.at_level(logging.WARNING):
        alerter.notify(ERROR, "svc: down", "body-text")
    failures = [r.getMessage() for r in caplog.records if "alert failed" in r.getMessage()]
    assert any(m.startswith("email alert failed") and "403" in m for m in failures)
    assert any(m.startswith("ntfy alert failed") for m in failures), "the other channel still ran"
    assert "not-a-real-password" not in caplog.text
    assert not any("body-text" in m for m in failures), "a failure line must not echo the body"


def test_a_proxy_that_is_down_is_a_channel_failure_not_a_crash(
        settings: AlertSettings, caplog: pytest.LogCaptureFixture) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    addr = f"127.0.0.1:{s.getsockname()[1]}"
    s.close()  # nothing listens here now
    write_email_config(settings, SMTP_HOST=MAIL_HOST, SMTP_PROXY=addr)
    with caplog.at_level(logging.WARNING):
        alerting.Alerter(settings).notify(ERROR, "svc: down", "body-text")
    assert "email" in caplog.text.lower()
    assert "not-a-real-password" not in caplog.text


@pytest.mark.parametrize("bad", ["proxy", "ftp://h:1", "http://u:p@h:1", "h:1/path"])
def test_a_malformed_proxy_setting_is_reported_and_refused(
        settings: AlertSettings, bad: str) -> None:
    write_email_config(settings, SMTP_PROXY=bad)
    cfg = AlertConfig.load(settings)
    assert cfg.setting_faults()
    with pytest.raises(ValueError, match="SMTP_PROXY"):
        alerting._send_email(cfg, alerting.SEVERITIES[ERROR], "t", "m")


def test_no_proxy_setting_still_uses_plain_smtplib(
        settings: AlertSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = []
    monkeypatch.setattr(alerting.smtplib, "SMTP", FakeSMTP)
    write_email_config(settings)
    alerting._send_email(AlertConfig.load(settings), alerting.SEVERITIES[ERROR], "t", "m")
    (smtp,) = FakeSMTP.instances
    assert smtp.calls == ["starttls", "login", "send_message"]
