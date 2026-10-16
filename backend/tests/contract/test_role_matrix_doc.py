"""The role matrix in docs/roles-and-permissions.md and the web app's role fixture must match the
code (authorization-model §9)."""

from scripts.role_matrix import DOCUMENT_PATH, FIXTURE_PATH, render_document, render_fixture

OUT_OF_DATE = "is out of date: run `uv run python -m scripts.role_matrix` from backend/"


def test_documented_role_matrix_matches_the_code() -> None:
    current = DOCUMENT_PATH.read_text(encoding="utf-8")
    assert current == render_document(current), f"docs/roles-and-permissions.md {OUT_OF_DATE}"


def test_web_app_role_fixture_matches_the_code() -> None:
    assert FIXTURE_PATH.read_text(encoding="utf-8") == render_fixture(), (
        f"frontend/src/test/role-permissions.json {OUT_OF_DATE}"
    )
