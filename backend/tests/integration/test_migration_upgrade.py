"""Upgrading a database migrated by an earlier release (docs/database-design.md §11).

The validators require every declared field, so a field added to a model makes every stored
document of that collection invalid until it holds the field. Regression (2026-10-23):
`erased_at` was added to accounts, employee records and document versions without a
migration, and `migrate` refused an existing local database ("5 document(s) in users do not
match the declared schema"). Migration 2 runs before the declarations and gives those documents
the field as null; this test starts from the previous release's validators and documents.
"""

import copy
import uuid
from datetime import UTC, date, datetime
from typing import Any

from app.metadata import MODELS
from app.modules.documents.models import DocumentVersion
from app.modules.identity.models import User, UserStatus
from app.modules.people.models import Employee, EmployeeStatus
from app.platform.db import Database, Document, create_client, to_document
from app.platform.files import FILE_INDEXES
from app.platform.schema import MIGRATIONS_COLLECTION, migrate, validator
from migrations import MIGRATIONS
from tests.conftest import MongoServer, fresh_database

ADDED = "erased_at"
ORGANIZATION = uuid.uuid4()
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def previous_validator(cls: type[Document]) -> dict[str, Any]:
    """The model's validator as the release before the field was added declared it."""
    rules = copy.deepcopy(validator(cls))
    schema = rules["$and"][0]["$jsonSchema"] if "$and" in rules else rules["$jsonSchema"]
    schema["required"].remove(ADDED)
    del schema["properties"][ADDED]
    return rules


def previous_document(obj: Document) -> dict[str, Any]:
    obj.before_write()
    stored = to_document(obj)
    del stored[ADDED]
    return stored


async def test_a_database_from_the_previous_release_migrates_and_keeps_its_data(
    migrated_mongo: MongoServer,
) -> None:
    name = fresh_database(migrated_mongo)
    user = User(
        organization_id=ORGANIZATION, email="test.upgrade@dev.example", status=UserStatus.INVITED.value
    )
    employee = Employee(
        organization_id=ORGANIZATION,
        employee_code="TEST-UPGRADE",
        legal_first_name="Test",
        legal_last_name="Upgrade",
        date_of_joining=date(2026, 1, 5),
        status=EmployeeStatus.ACTIVE.value,
    )
    version = DocumentVersion(
        organization_id=ORGANIZATION,
        document_id=uuid.uuid4(),
        version_no=1,
        storage_key=f"documents/{uuid.uuid4()}",
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256=b"0" * 32,
        uploaded_by=user.id,
        uploaded_at=NOW,
    )
    with migrated_mongo.admin() as client:
        db = client[name]
        for obj in (user, employee, version):
            cls = type(obj)
            db.create_collection(
                cls.__collection__, validator=previous_validator(cls), validationLevel="strict"
            )
            # Valid under the previous release's validator, as the old code wrote it.
            db[cls.__collection__].insert_one(previous_document(obj))
        db[MIGRATIONS_COLLECTION].insert_one(
            {"_id": "v0001", "version": 1, "name": "baseline", "applied_at": NOW}
        )

    database = Database(
        create_client(migrated_mongo.uri("hrms_migrator"), application_name="test-upgrade"), name
    )
    try:
        report = await migrate(database, MODELS, MIGRATIONS, FILE_INDEXES)
        assert report.applied_versions == [2]
        again = await migrate(database, MODELS, MIGRATIONS, FILE_INDEXES)
        assert again.applied_versions == []
    finally:
        await database.dispose()

    with migrated_mongo.admin() as client:
        db = client[name]
        for obj in (user, employee, version):
            cls = type(obj)
            [stored] = list(db[cls.__collection__].find({}))
            # The field is there, not erased; everything else is as it was.
            assert stored.pop(ADDED) is None
            assert stored == previous_document(obj)
            # The current validator is in force, strict, and every document matches it.
            options = db.command("listCollections", filter={"name": cls.__collection__})["cursor"][
                "firstBatch"
            ][0]
            assert options["options"]["validator"] == validator(cls)
            assert options["options"]["validationLevel"] == "strict"
            assert db[cls.__collection__].count_documents({"$nor": [validator(cls)]}) == 0
