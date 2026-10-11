"""The committed contract (api/openapi.json) must match the application."""

from typing import get_args

from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel

from app.main import _build
from scripts.export_openapi import CONTRACT_PATH, render


def test_committed_openapi_matches_application() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render(), (
        "api/openapi.json is out of date: run `uv run python -m scripts.export_openapi` from backend/"
    )


def _models(annotation: object, found: dict[str, set[type]]) -> None:
    """Every Pydantic model reachable from a type annotation, by schema name."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        seen = found.setdefault(annotation.__name__, set())
        if annotation in seen:
            return
        seen.add(annotation)
        for field in annotation.model_fields.values():
            _models(field.annotation, found)
        return
    for argument in get_args(annotation):
        _models(argument, found)
    value = getattr(annotation, "__value__", None)
    if value is not None:
        _models(value, found)


def test_no_two_models_share_a_schema_name() -> None:
    """Two models with one name would silently replace each other in the contract."""
    found: dict[str, set[type]] = {}
    routes = [context.route for context in iter_route_contexts(_build(docs_enabled=False).routes)]
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        _models(route.response_model, found)
        if route.body_field is not None:
            _models(route.body_field.field_info.annotation, found)
    assert "LeaveDayView" in found
    assert "DayView" in found
    clashes = {name: models for name, models in found.items() if len(models) > 1}
    assert clashes == {}
