"""Organizations: the tenants of the system (docs/authorization-model.md §1.1, ADR-030).

Every record that belongs to a company carries its `organization_id`, and every unit of work
belongs to one organization (`app.platform.db`). An account belongs to exactly one
organization; its email address is unique across all of them, so signing in needs no
organization name. Organizations are created only by an operator command
(`python -m app.cli create-organization`), never through the API: no account can create,
list or enter another organization.
"""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import CASE_INSENSITIVE, Document, Index, asc, utcnow
from app.platform.ids import uuid7


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


@dataclasses.dataclass(kw_only=True, eq=False)
class Organization(Document):
    """`organizations`. Its `_id` is the `organization_id` of everything the company owns."""

    __collection__ = "organizations"
    __tenant_scoped__ = False
    __deletable__ = False
    __indexes__ = (Index(asc("slug"), name="uq_organizations_slug", unique=True, collation=CASE_INSENSITIVE),)
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "name": {"minLength": 1, "maxLength": 200},
        "slug": {"pattern": "^[a-z0-9][a-z0-9-]{1,62}$"},
    }

    id: uuid.UUID = dataclasses.field(default_factory=uuid7)
    name: str
    slug: str
    status: OrganizationStatus = OrganizationStatus.ACTIVE
    created_at: datetime = dataclasses.field(default_factory=utcnow)
