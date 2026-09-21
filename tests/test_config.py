import os

from quant_mcp.config import load_env_file


def test_load_env_file_without_overriding_existing_env(tmp_path, monkeypatch) -> None:
    for key in ["SMOLAGENTS_MODEL_ID", "SMOLAGENTS_API_BASE", "SMOLAGENTS_MAX_STEPS"]:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'SMOLAGENTS_MODEL_ID="office-model"',
                "SMOLAGENTS_API_BASE=https://office.invalid/v1",
                "SMOLAGENTS_MAX_STEPS=8",
                "EXISTING=value-from-file",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "value-from-env")

    loaded = load_env_file(env_file)

    assert loaded["SMOLAGENTS_MODEL_ID"] == "office-model"
    assert os.environ["SMOLAGENTS_API_BASE"] == "https://office.invalid/v1"
    assert os.environ["SMOLAGENTS_MAX_STEPS"] == "8"
    assert os.environ["EXISTING"] == "value-from-env"
