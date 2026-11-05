"""Login is the only sign-in identifier; External ID is an optional integration field."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_v3 import v3_admin_login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def _create(client: TestClient, **fields: object):  # type: ignore[no-untyped-def]
    payload: dict[str, object] = {"full_name": "Person", "temporary_password": "Temporary-pass-123", **fields}
    return client.post("/api/v3/admin/accounts", json=payload)


@pytest.mark.parametrize("external_id", [None, "", "   "])
def test_account_without_external_id_gets_generated_one(client: TestClient, external_id: str | None) -> None:
    v3_admin_login(client)
    fields: dict[str, object] = {"login": "anna.petrova@corp"}
    if external_id is not None:
        fields["external_id"] = external_id
    created = _create(client, **fields)
    assert created.status_code == 201
    body = created.json()
    assert body["login"] == "anna.petrova@corp"
    assert re.fullmatch(r"bg-[0-9a-f]{12}", body["external_id"])
    assert body["external_id"] != body["login"]

    client.cookies.delete("biogate_session")
    credentials = {"login": "anna.petrova@corp", "password": "Temporary-pass-123"}
    signed_in = client.post("/api/v3/auth/password", json=credentials)
    assert signed_in.status_code == 200


def test_generated_external_ids_are_unique(client: TestClient) -> None:
    v3_admin_login(client)
    ids = {_create(client, login=f"user-{index}").json()["external_id"] for index in range(5)}
    assert len(ids) == 5


def test_explicit_external_id_is_kept_and_cannot_sign_in(client: TestClient) -> None:
    v3_admin_login(client)
    created = _create(client, login="ivan", external_id="HR-0042")
    assert created.status_code == 201
    assert created.json()["external_id"] == "HR-0042"

    duplicate = _create(client, login="ivan-2", external_id="HR-0042")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason_code"] == "EXTERNAL_ID_ALREADY_EXISTS"

    client.cookies.delete("biogate_session")
    wrong_identifier = client.post("/api/v3/auth/password", json={"login": "HR-0042", "password": "Temporary-pass-123"})
    assert wrong_identifier.status_code == 401


def test_login_is_still_required(client: TestClient) -> None:
    v3_admin_login(client)
    assert _create(client, external_id="only-external").status_code == 422


def test_admin_create_form_has_one_required_login_and_optional_integration_id() -> None:
    template = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    start = template.index('<form id="create-user"')
    form = template[start : template.index("</form>", start)]
    login_input = re.search(r'<input name="login"[^>]*>', form)
    external_input = re.search(r'<input name="external_id"[^>]*>', form)
    assert login_input and " required" in login_input.group(0)
    assert external_input and " required" not in external_input.group(0)
    integration = form[form.index('<details class="wide integration-fields">') : form.index("</details>")]
    assert 'name="external_id"' in integration
    assert 'data-i18n="externalIdHint"' in integration


def test_admin_labels_external_id_consistently() -> None:
    admin = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "'User ID'" not in admin and "'External ID'" not in admin
    assert "login: values.login || values.external_id" not in admin
    locales = (STATIC / "locales.js").read_text(encoding="utf-8")
    assert "external_id:'Логин'" not in locales and "external_id:'Login'" not in locales
    assert "external_id:'Внешний ID'" in locales and "external_id:'External ID'" in locales
    for key in ("externalId", "integration", "externalIdHint", "loginHint"):
        assert locales.count(f"{key}:'") == 2, key
