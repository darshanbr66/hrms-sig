"""Write the committed API contract: `uv run python -m scripts.export_openapi`."""

import json
from pathlib import Path

from app.main import openapi_document

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "api" / "openapi.json"


def render() -> str:
    return json.dumps(openapi_document(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    CONTRACT_PATH.write_text(render(), encoding="utf-8", newline="\n")
