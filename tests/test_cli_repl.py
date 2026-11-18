from click.testing import CliRunner

from aigineering.cli.main import cli


def test_empty_slash_keeps_repl_open(tmp_path, monkeypatch):
    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    result = CliRunner().invoke(
        cli, ["--workspace", str(tmp_path), "repl"], input="/\n/help\n/quit\n"
    )

    assert result.exit_code == 0, result.output
    assert "Available commands:" in result.output
    assert "Goodbye." in result.output
