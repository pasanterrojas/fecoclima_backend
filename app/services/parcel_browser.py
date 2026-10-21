"""Anonymous parcel access is a signed browser capability, separate from CMS login."""
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request

from app.core.security import decode_token

COOKIE_NAME = "fecoclima_parcel_access"
TOKEN_TYPE = "parcel_browser"


@dataclass(frozen=True)
class BrowserParcelActor:
    browser_id: UUID
    # Nullable foreign keys distinguish public activity from administrator accounts.
    id: None = None


def browser_actor(request: Request) -> BrowserParcelActor:
    token = request.headers.get("X-Parcel-Access") or request.cookies.get(COOKIE_NAME)
    try:
        claims = decode_token(token or "", TOKEN_TYPE)
        identifier = UUID(claims["sub"])
        if not identifier.int:
            raise ValueError("Empty browser")
        # Mutations require a non-simple custom header, never cookies alone.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("X-Parcel-Session") != str(identifier):
                raise ValueError("Browser header mismatch")
        return BrowserParcelActor(identifier)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "No se pudo recuperar este navegador. Recargue el portal público.") from None


def public_payload(value):
    """Keep images, media and historical snapshots within the same public scope."""
    if isinstance(value, str) and value.startswith("/parcels/"):
        return "/public" + value
    if isinstance(value, dict):
        return {key: public_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    return value
