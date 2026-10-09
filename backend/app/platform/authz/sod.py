"""Separation-of-duties rules enforced regardless of permissions held (docs/authorization-model.md §6).

Rules that belong to later modules (leave, attendance, payroll) are added with them. Each
refusal carries a stable code so the client can explain it.
"""

import uuid
from enum import StrEnum

from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied


class SodRule(StrEnum):
    OWN_ROLES = "sod.own_roles"  # SOD-3
    OWN_ACCOUNT = "sod.own_account"  # SOD-4
    OWN_REQUEST = "sod.own_request"  # SOD-8, SOD-9: nobody decides a request they made or that is about them
    SUPER_ADMIN_DATA_ROLE = "sod.super_admin_data_role"  # SOD-10


def refuse_self(actor: Actor, target_user_id: uuid.UUID, rule: SodRule) -> None:
    """SOD-3 and SOD-4: an administrative action on another account, never one's own."""
    if actor.user_id == target_user_id:
        raise AccessDenied(code=rule.value)
