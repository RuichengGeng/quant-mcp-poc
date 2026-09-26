import json

from quant_mcp.testing.scenarios import load_suite


def test_load_json_suite(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({
        "expected_tools": ["echo"],
        "cases": [{"id": "echoes", "tool": "echo", "arguments": {"value": "x"}}],
    }))

    expected, cases = load_suite(path)

    assert expected == ["echo"]
    assert cases[0].id == "echoes"
    assert cases[0].arguments == {"value": "x"}


def test_load_prompt_scenario(tmp_path):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({
        "cases": [{
            "id": "unsupported",
            "type": "prompt",
            "prompt": "What is the weather?",
            "replay": {"response": "I cannot answer that.", "tool_calls": []},
            "assertions": {"expected_tool_calls": []},
        }],
    }))

    _, cases = load_suite(path)

    assert cases[0].type == "prompt"
    assert cases[0].prompt == "What is the weather?"
    assert cases[0].replay["response"] == "I cannot answer that."
