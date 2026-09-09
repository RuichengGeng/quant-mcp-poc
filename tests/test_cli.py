from quant_mcp.cli import main


def test_cli_list_runs(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["quant-cli", "list"])

    main()

    captured = capsys.readouterr()
    assert "artifacts" in captured.out.lower() or "task_" in captured.out

