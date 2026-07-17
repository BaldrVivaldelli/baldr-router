from __future__ import annotations

from baldr_router.kiro_cli import _try_parse_json


def test_try_parse_json_accepts_exact_json() -> None:
    assert _try_parse_json('{"status":"reviewed","summary":"ok"}') == {
        "status": "reviewed",
        "summary": "ok",
    }


def test_try_parse_json_extracts_final_report_after_ansi_tool_activity() -> None:
    output = (
        "Reading file: \x1b[mREADME.md\x1b[0m\n"
        "\x1b[m ✓ \x1b[0mSuccessfully read 20 bytes\n\n"
        "\x1b[m> \x1b[0m"
        '{"status":"reviewed","summary":"Marcador \x1b[mconfirmado\x1b[0m",'
        '"verification_evidence":["README.md:4"],"review_decision":"approved"}'
    )

    assert _try_parse_json(output) == {
        "status": "reviewed",
        "summary": "Marcador confirmado",
        "verification_evidence": ["README.md:4"],
        "review_decision": "approved",
    }
