"""The local start script's safety check refuses anything but a named local database."""

import pytest

from scripts.check_local_target import problems

LOCAL = "mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true"
ATLAS = "mongodb+srv://dev:pw@cluster0.example.mongodb.net/?retryWrites=true"


def env(**values: str) -> dict[str, str]:
    return {"APP_ENV": "local", "MONGODB_URI": LOCAL, "MONGODB_DATABASE": "hrms_local"} | values


def test_a_local_database_on_this_machine_passes() -> None:
    assert problems(env(), "hrms_local", allow_remote=False) == []
    assert (
        problems(env(MONGODB_URI="mongodb://[::1]:27017,localhost:27018/"), "hrms_local", allow_remote=False)
        == []
    )


@pytest.mark.parametrize(
    ("environ", "database", "message"),
    [
        (env(APP_ENV="production"), "hrms_local", "APP_ENV"),
        (env(MONGODB_DATABASE="hrms"), "hrms", "production database"),
        (env(), "hrms_other", "MONGODB_DATABASE is not hrms_other"),
        (env(MONGODB_URI=ATLAS), "hrms_local", "this machine"),
        (env(MONGODB_URI="mongodb://127.0.0.1:27017,db.example.com:27017/"), "hrms_local", "this machine"),
        (env(MONGODB_URI="postgresql://127.0.0.1/hrms"), "hrms_local", "mongodb://"),
    ],
)
def test_anything_else_is_refused(environ: dict[str, str], database: str, message: str) -> None:
    assert any(message in item for item in problems(environ, database, allow_remote=False))


def test_a_remote_cluster_needs_explicit_consent_and_never_the_production_name() -> None:
    assert problems(env(MONGODB_URI=ATLAS), "hrms_local", allow_remote=True) == []
    found = problems(env(MONGODB_URI=ATLAS, MONGODB_DATABASE="hrms"), "hrms", allow_remote=True)
    assert any("production database" in item for item in found)
