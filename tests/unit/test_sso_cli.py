"""Native pairing commands must not operate on SSO browser identities."""

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import IndexConfig, ServerConfig


def test_pairing_cli_is_disabled_in_trusted_proxy_mode(tmp_path):
    cfg = SimpleNamespace(
        index=IndexConfig(tmp_path / "index", tmp_path / "models"),
        server=ServerConfig(auth_mode="trusted_proxy"),
    )
    with patch("autodj.cli._load_cfg_or_exit", return_value=cfg):
        result = CliRunner().invoke(cli, ["devices", "pairing-code"])
    assert result.exit_code == 1
    assert "SSO" in result.output
    assert not cfg.index.index_dir.exists()
