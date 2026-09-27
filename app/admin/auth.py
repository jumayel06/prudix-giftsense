"""HTTP Basic Auth for the internal admin dashboard.

Single shared username/password from env vars. Sufficient for a one-operator
dashboard you visit from your laptop. Never use this for merchant-facing routes.
"""

import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from core.config import settings

_basic = HTTPBasic(realm="Prudix Admin")


def require_admin(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    """Dependency that 401s unless the request has valid admin credentials.

    Raises 503 if no admin password is configured (avoids accidentally serving
    the dashboard with empty credentials in misconfigured environments).
    """
    if not settings.internal_admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin dashboard is not configured (INTERNAL_ADMIN_PASSWORD missing).",
        )

    expected_user = settings.internal_admin_username.encode("utf-8")
    expected_pass = settings.internal_admin_password.encode("utf-8")
    given_user = credentials.username.encode("utf-8")
    given_pass = credentials.password.encode("utf-8")

    # constant-time compare to avoid timing attacks
    user_ok = secrets.compare_digest(given_user, expected_user)
    pass_ok = secrets.compare_digest(given_pass, expected_pass)

    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Prudix Admin"'},
        )
    return credentials.username
