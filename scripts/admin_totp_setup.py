"""Generate (or check) the admin dashboard's TOTP secret.

    .venv/bin/python scripts/admin_totp_setup.py            # new secret + setup URI
    .venv/bin/python scripts/admin_totp_setup.py --check     # show the current code for ADMIN_TOTP_SECRET

Add the secret to your authenticator app (Google Authenticator, 1Password,
Authy…: "enter a setup key", time-based), confirm the codes match with --check,
then set ADMIN_TOTP_SECRET in Railway (prod) / .env (local). Keep a copy in your
password manager: losing it means removing ADMIN_TOTP_SECRET to get back in.
"""
import base64
import os
import secrets
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admin.auth import TOTP_STEP, totp_at  # noqa: E402


def main() -> None:
    if "--check" in sys.argv:
        from core.config import settings
        secret = settings.admin_totp_secret.strip()
        if not secret:
            sys.exit("ADMIN_TOTP_SECRET is not set in this environment.")
        print(f"Current code: {totp_at(secret, int(time.time() // TOTP_STEP))}  (compare with your app)")
        return

    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    label = quote("GiftSense Admin:admin")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={quote('GiftSense Admin')}&digits=6&period=30"
    grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    print("ADMIN_TOTP_SECRET =", secret)
    print()
    print("Setup key (type into your authenticator app, time-based):", grouped)
    print("Or paste this URI (1Password / most apps accept it):")
    print(uri)
    print()
    print(f"Code right now: {totp_at(secret, int(time.time() // TOTP_STEP))}  (should match your app)")


if __name__ == "__main__":
    main()
