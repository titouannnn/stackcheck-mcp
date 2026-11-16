import pytest

from stackcheck_mcp.__main__ import is_loopback, parse_args


def test_defaults() -> None:
    args = parse_args([])
    assert not args.http and args.host == "127.0.0.1" and args.call_timeout == 45.0


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback(host)
    assert parse_args(["--http", "--host", host]).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_remote_bind_needs_explicit_opt_in(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert not is_loopback(host)
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--http", "--host", host])
    assert exit_info.value.code == 2
    assert "unauthenticated server" in capsys.readouterr().err
    assert parse_args(["--http", "--host", host, "--allow-remote"]).allow_remote
    assert parse_args(["--host", host]).host == host  # stdio ignores --host
