"""The committed contract (api/openapi.json) must match the application."""

from scripts.export_openapi import CONTRACT_PATH, render


def test_committed_openapi_matches_application() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render(), (
        "api/openapi.json is out of date: run `uv run python -m scripts.export_openapi` from backend/"
    )
