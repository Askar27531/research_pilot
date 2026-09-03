import base64
import hashlib
import hmac
import json

from app.core.config import get_settings
from app.documents.errors import DocumentSecurityError


def issue_token(project_id: str, kind: str, identifier: str) -> str:
    payload = json.dumps({"p": project_id, "k": kind, "i": identifier},
                         separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(get_settings().resource_token_secret.encode(), encoded,
                         hashlib.sha256).digest()
    return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def read_token(token: str, project_id: str, expected_kind: str | None = None) -> dict[str, str]:
    try:
        encoded, supplied = token.encode().split(b".", 1)
        expected = hmac.new(get_settings().resource_token_secret.encode(), encoded,
                            hashlib.sha256).digest()
        supplied_bytes = base64.urlsafe_b64decode(supplied + b"=" * (-len(supplied) % 4))
        if not hmac.compare_digest(expected, supplied_bytes):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(
            encoded + b"=" * (-len(encoded) % 4)).decode())
    except Exception as exc:
        raise DocumentSecurityError("Invalid workspace resource token") from exc
    if payload.get("p") != project_id or expected_kind and payload.get("k") != expected_kind:
        raise DocumentSecurityError("Resource token does not belong to this project")
    return payload
