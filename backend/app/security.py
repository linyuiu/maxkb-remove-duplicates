import hashlib
import os
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

passwords = PasswordHash.recommended()


def cipher():
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("必须配置 ENCRYPTION_KEY（Fernet key）")
    return Fernet(key.encode())


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def content_hash(title, content):
    import json

    return digest(json.dumps([title, content], ensure_ascii=False))


def schedule_confirmation(user_id, task_id, values, token=None):
    """Bind a ten-minute schedule-save approval to the complete normalized form."""
    import json
    from cryptography.fernet import InvalidToken

    payload = {
        "purpose": "schedule-save",
        "user_id": user_id,
        "task_id": task_id,
        "values": {k: v for k, v in values.items() if k != "next_fire"},
    }
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if token is None:
        return cipher().encrypt(raw).decode()
    try:
        actual = cipher().decrypt(token.encode(), ttl=600)
        if actual != raw:
            raise ValueError()
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise ValueError("定时任务预估已过期或配置已变化，请重新预估并确认") from None
