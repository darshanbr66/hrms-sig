"""The complete SQLAlchemy metadata: every module's models and the platform's tables.

Imported by Alembic's environment and by the schema drift test, so `Base.metadata`
describes every table the migrations create. Modules never import each other's models;
this composition root is the one place that imports them all.
"""

from app.modules.access import models as access_models
from app.modules.identity import models as identity_models
from app.modules.org import models as org_models
from app.modules.people import models as people_models
from app.platform.audit import tables as audit_tables
from app.platform.db import Base

metadata = Base.metadata

__all__ = ["access_models", "audit_tables", "identity_models", "metadata", "org_models", "people_models"]
