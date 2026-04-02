#!/usr/bin/env python3
"""
WattiVahti Initial Refresh Token Retrieval

Performs Azure B2C PKCE authorization code login flow to obtain
the initial refresh_token for WattiVahti API access.

Run this once when setting up the project or when the stored refresh_token
has expired and can no longer be renewed.

Usage:
    WATTIVAHTI_USERNAME=user@example.com WATTIVAHTI_PASSWORD=secret uv run get_token.py

The retrieved token is saved to refresh_token.txt (or REFRESH_TOKEN_FILE env var).
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Azure B2C constants shared with sync.py
_CLIENT_ID = "84ebdb93-9ea6-42c7-bd7d-302abf7556fa"
_REDIRECT_URI = "https://www.wattivahti.fi/wattivahti/"
_AUTHORIZE_URL = (
    "https://pesv.b2clogin.com/pesv.onmicrosoft.com/b2c_1_tunnistus_signinv2/oauth2/v2.0/authorize"
)
_TOKEN_URL = (
    "https://pesv.b2clogin.com/pesv.onmicrosoft.com/b2c_1_tunnistus_signinv2/oauth2/v2.0/token"
)
_INITIAL_SCOPE = "offline_access openid profile"
_FULL_SCOPE = "https://pesv.onmicrosoft.com/salpa/customer.read openid profile offline_access"

logger = logging.getLogger(__name__)


def _make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _parse_b2c_settings(html: str) -> dict:
    """Extract the SETTINGS object that Azure B2C embeds in the login page HTML."""
    match = re.search(r"var SETTINGS\s*=\s*({.*?});", html, re.DOTALL)
    if not match:
        raise RuntimeError(
            "Could not find SETTINGS variable in Azure B2C login page HTML. "
            "The login page structure may have changed."
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse Azure B2C SETTINGS JSON: {exc}") from exc


def _parse_b2c_field_ids(html: str) -> tuple[str, str]:
    """
    Extract username and password field IDs from the SA_FIELDS variable in the HTML.

    Azure B2C embeds the form field configuration as ``var SA_FIELDS = {...};``.
    Parsing this instead of hardcoding field names makes the code resilient to
    B2C policy changes that rename the login identifier field.

    Returns:
        (username_field_id, password_field_id)
    """
    match = re.search(r"var SA_FIELDS\s*=\s*({.*?});", html, re.DOTALL)
    if not match:
        # Fall back to the well-known Azure B2C defaults if SA_FIELDS is absent
        logger.warning(
            "SA_FIELDS not found in Azure B2C HTML; "
            "falling back to default field IDs (logonIdentifier / password)"
        )
        return "logonIdentifier", "password"

    try:
        sa_fields = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse SA_FIELDS JSON (%s); using default field IDs", exc)
        return "logonIdentifier", "password"

    username_id = ""
    password_id = ""
    for field in sa_fields.get("AttributeFields", []):
        if field.get("IS_PASSWORD"):
            password_id = field["ID"]
        elif not username_id:
            username_id = field["ID"]

    if not username_id or not password_id:
        raise RuntimeError(
            f"Could not identify username/password fields in SA_FIELDS: "
            f"{[f['ID'] for f in sa_fields.get('AttributeFields', [])]}"
        )

    return username_id, password_id


def fetch_initial_refresh_token(username: str, password: str) -> str:
    """
    Perform Azure B2C PKCE authorization code flow to obtain a refresh_token.

    The flow:
    1. GET /authorize with PKCE params → HTML login page with embedded CSRF/tx state
    2. POST /SelfAsserted  → submit credentials
    3. GET /api/.../confirmed → redirects to redirect_uri with authorization code
    4. POST /token (authorization_code grant) → initial refresh_token
    5. POST /token (refresh_token grant, full scope) → final refresh_token

    Args:
        username: WattiVahti account email address
        password: WattiVahti account password

    Returns:
        The refresh_token string

    Raises:
        RuntimeError: If any step of the login flow fails
    """
    code_verifier, code_challenge = _make_pkce_pair()

    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # --- Step 1: Initiate authorization, get B2C CSRF token and transaction ID ---
    logger.info("Initiating Azure B2C authorization flow...")
    resp = session.get(
        _AUTHORIZE_URL,
        params={
            "client_id": _CLIENT_ID,
            "scope": _INITIAL_SCOPE,
            "redirect_uri": _REDIRECT_URI,
            "response_mode": "fragment",
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "nonce": secrets.token_urlsafe(16),
            "state": secrets.token_urlsafe(16),
            "ui_locales": "fi",
        },
        timeout=30,
    )
    resp.raise_for_status()

    try:
        settings = _parse_b2c_settings(resp.text)
        csrf_token: str = settings["csrf"]
        trans_id: str = settings["transId"]
        policy: str = settings["hosts"]["policy"]
        tenant_path: str = settings["hosts"]["tenant"]
        api_name: str = settings["api"]
    except KeyError as exc:
        raise RuntimeError(
            f"Azure B2C SETTINGS is missing expected key {exc}. "
            f"The B2C page structure may have changed."
        ) from exc

    username_field, password_field = _parse_b2c_field_ids(resp.text)

    # --- Step 2: Submit credentials to SelfAsserted endpoint ---
    logger.info("Submitting credentials...")
    self_asserted_url = f"https://pesv.b2clogin.com{tenant_path}/SelfAsserted"
    resp2 = session.post(
        self_asserted_url,
        params={"tx": trans_id, "p": policy},
        data={
            "request_type": "RESPONSE",
            username_field: username,
            password_field: password,
        },
        headers={"X-CSRF-TOKEN": csrf_token},
        timeout=30,
    )
    resp2.raise_for_status()

    login_result = resp2.json()
    if login_result.get("status") != "200":
        raise RuntimeError(f"Login failed (invalid credentials or account locked?): {login_result}")

    # --- Step 3: Confirm login, triggering the redirect with authorization code ---
    logger.info("Completing authorization flow...")
    confirmed_url = f"https://pesv.b2clogin.com{tenant_path}/api/{api_name}/confirmed"
    resp3 = session.get(
        confirmed_url,
        params={"csrf_token": csrf_token, "tx": trans_id, "p": policy},
        allow_redirects=False,
        timeout=30,
    )

    if resp3.status_code not in (301, 302, 303, 307, 308):
        raise RuntimeError(
            f"Expected redirect from confirmed endpoint, got {resp3.status_code}: "
            f"{resp3.text[:500]}"
        )

    location = resp3.headers.get("Location", "")
    if "#" not in location:
        raise RuntimeError(
            f"Redirect does not contain fragment with authorization code: {location}"
        )

    fragment = location.split("#", 1)[1]
    fragment_params = dict(part.split("=", 1) for part in fragment.split("&") if "=" in part)
    auth_code = fragment_params.get("code", "")
    if not auth_code:
        raise RuntimeError(f"Authorization code not found in redirect fragment: {fragment_params}")

    # --- Step 4: Exchange authorization code for tokens ---
    logger.info("Exchanging authorization code for tokens...")
    resp4 = session.post(
        _TOKEN_URL,
        data={
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": _INITIAL_SCOPE,
            "code": auth_code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "client_info": "1",
        },
        timeout=30,
    )
    resp4.raise_for_status()

    initial_refresh_token: str = resp4.json().get("refresh_token", "")
    if not initial_refresh_token:
        raise RuntimeError(f"No refresh_token in authorization code response: {resp4.text[:500]}")

    # --- Step 5: Upgrade to full-scope refresh token ---
    logger.info("Upgrading to full-scope token...")
    resp5 = session.post(
        _TOKEN_URL,
        data={
            "client_id": _CLIENT_ID,
            "scope": _FULL_SCOPE,
            "grant_type": "refresh_token",
            "refresh_token": initial_refresh_token,
            "client_info": "1",
        },
        timeout=30,
    )
    resp5.raise_for_status()

    final_refresh_token: str = resp5.json().get("refresh_token", "")
    if not final_refresh_token:
        raise RuntimeError(f"No refresh_token in scope upgrade response: {resp5.text[:500]}")

    return final_refresh_token


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    load_dotenv()

    username = os.environ.get("WATTIVAHTI_USERNAME", "")
    password = os.environ.get("WATTIVAHTI_PASSWORD", "")

    if not username or not password:
        print(
            "Error: WATTIVAHTI_USERNAME and WATTIVAHTI_PASSWORD environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    token_file = Path(os.environ.get("REFRESH_TOKEN_FILE", "refresh_token.txt"))

    try:
        refresh_token = fetch_initial_refresh_token(username, password)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        sys.exit(1)

    token_file.write_text(refresh_token)
    print(f"Refresh token saved to {token_file.absolute()}")


if __name__ == "__main__":
    main()
