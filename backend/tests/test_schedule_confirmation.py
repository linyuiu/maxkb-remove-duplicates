from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.main import TaskBody, task_values, require_schedule_confirmation
from app.security import schedule_confirmation, cipher


def form():
    return TaskBody(
        name="night scan",
        source_id="source",
        knowledge_ids=["00000000-0000-0000-0000-000000000001"],
        enabled=True,
        cron="0 0 * * *",
    )


def test_enabled_schedule_cannot_save_without_confirmation():
    body = form()
    with pytest.raises(HTTPException) as exc:
        require_schedule_confirmation(
            body, task_values(body), SimpleNamespace(id="user")
        )
    assert exc.value.status_code == 409


def test_confirmation_binds_form_user_and_create_or_edit():
    body = form()
    values = task_values(body)
    token = schedule_confirmation("user", None, values)
    approved = body.model_copy(update={"save_estimate": token})
    require_schedule_confirmation(
        approved, task_values(approved), SimpleNamespace(id="user")
    )
    with pytest.raises(ValueError):
        schedule_confirmation("other", None, values, token)
    with pytest.raises(ValueError):
        schedule_confirmation("user", "task-id", values, token)
    for field, value in [
        ("cron", "0 3 * * *"),
        ("threshold", 0.95),
        ("batch_size", 64),
    ]:
        changed = approved.model_copy(update={field: value})
        with pytest.raises(HTTPException):
            require_schedule_confirmation(
                changed, task_values(changed), SimpleNamespace(id="user")
            )


def test_expired_confirmation_is_rejected_and_disabling_is_immediate():
    body = form()
    values = task_values(body)
    token = schedule_confirmation("user", None, values)
    expired = (
        cipher()
        .encrypt_at_time(cipher().decrypt(token.encode()), current_time=1)
        .decode()
    )
    with pytest.raises(ValueError):
        schedule_confirmation("user", None, values, expired)
    disabled = body.model_copy(update={"enabled": False})
    require_schedule_confirmation(
        disabled, task_values(disabled), SimpleNamespace(id="user")
    )
