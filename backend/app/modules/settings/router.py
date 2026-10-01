"""HTTP routes for settings (docs/api-architecture.md §8: `GET /settings`, `PUT /settings/{key}`)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, StrictInt

from app.modules.settings.service import SettingsService, SettingState
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.route import requires

router = APIRouter(prefix="/api/v1", tags=["settings"])


def service(request: Request) -> SettingsService:
    value: SettingsService = request.app.state.settings_service
    return value


Service = Annotated[SettingsService, Depends(service)]


class SettingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    description: str
    value: int
    default: int
    minimum: int
    maximum: int
    unit: Literal["attempts", "minutes", "hours", "days"]
    overridden: bool
    # The permission needed to change it; both need step-up.
    permission: Literal["security.settings.manage", "settings.manage"]


class SettingList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SettingView]


class SettingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A whole number within the setting's bounds, or null to restore the default.
    value: StrictInt | None


def _view(state: SettingState) -> SettingView:
    setting = state.setting
    return SettingView(
        key=setting.key,
        description=setting.description,
        value=state.value,
        default=setting.default,
        minimum=setting.minimum,
        maximum=setting.maximum,
        unit=setting.unit.value,
        overridden=state.overridden,
        permission=setting.permission,
    )


@router.get("/settings", response_model=SettingList)
async def list_settings(
    settings: Service, _actor: Annotated[Actor, requires("settings.read")]
) -> SettingList:
    return SettingList(items=[_view(state) for state in await settings.list()])


@router.put("/settings/{key}", response_model=SettingView)
async def change_setting(
    key: Annotated[str, Path(max_length=100)],
    body: SettingChange,
    request: Request,
    settings: Service,
    actor: Annotated[Actor, requires("settings.manage")],
) -> SettingView:
    """`security.*` keys additionally need `security.settings.manage`; both need step-up."""
    return _view(await settings.change(actor, RequestContext.from_request(request), key, body.value))
