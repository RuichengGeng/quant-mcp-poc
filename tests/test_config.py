import os

from quant_mcp.config import load_env_file


def test_load_env_file_without_overriding_existing_env(tmp_path, monkeypatch) -> None:
    for key in ["QUANT_MCP_AGENT", "PI_PROVIDER", "PI_MODEL", "PI_THINKING"]:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "QUANT_MCP_AGENT=pi",
                "PI_PROVIDER=google",
                'PI_MODEL="gemini-3.8"',
                "PI_THINKING=medium",
                "EXISTING=value-from-file",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "value-from-env")

    loaded = load_env_file(env_file)

    assert loaded["PI_MODEL"] == "gemini-3.8"
    assert os.environ["QUANT_MCP_AGENT"] == "pi"
    assert os.environ["PI_PROVIDER"] == "google"
    assert os.environ["EXISTING"] == "value-from-env"
