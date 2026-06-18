"""
OAuth provider creation for the Obot MCP gateway.

Same pattern as apps/Stock-Analysis and apps/MCP-Trip-Planner — every server
URL is reached over streamable_http and authenticated with OAuth 2.1 / PKCE.
Tokens are persisted to disk so refresh tokens survive between runs.

Token refresh strategy
----------------------
The mcp library loads the stored token but never sets its internal
``token_expiry_time``, so ``is_token_valid()`` always returns True regardless
of age.  The library then sends the expired token, receives a 401, and falls
straight into the interactive PKCE flow instead of trying the refresh token.

We work around this by proactively refreshing the token *before* handing
control to the mcp library.  ``proactive_refresh(client_name)`` checks whether
the stored token was issued more than ``expires_in - REFRESH_MARGIN`` seconds
ago; if so, it posts a refresh-token grant directly to the Obot token endpoint
and saves the new token.  The mcp library then finds a fresh token on disk and
uses it without prompting.

``proactive_refresh`` is called automatically inside ``create_oauth_provider``
so callers don't have to change anything.
"""

import os
import json
import time
import urllib.request
import urllib.parse
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientMetadata,
    OAuthClientInformationFull,
    OAuthToken,
    AnyUrl,
)

# Refresh the token this many seconds before it actually expires
REFRESH_MARGIN = 60  # seconds

TOKEN_ENDPOINT = "https://cbg-obot.com/oauth/token"


async def handle_redirect(auth_url: str) -> None:
    print(f"\n{'='*60}")
    print("AUTHORIZATION REQUIRED")
    print("=" * 60)
    print(f"\nVisit this URL to authorize:\n{auth_url}\n")


async def handle_callback() -> tuple[str, str | None]:
    callback_url = input("Paste the callback URL here: ").strip()
    params = parse_qs(urlparse(callback_url).query)
    return params["code"][0], params.get("state", [None])[0]


class FileTokenStorage(TokenStorage):
    def __init__(self, server_name: str):
        token_dir = os.path.expanduser(
            os.getenv("TOKEN_STORAGE_DIR", "~/.github_agent_tokens")
        )
        os.makedirs(token_dir, exist_ok=True)
        safe_name = server_name.replace(" ", "_").replace("/", "_").replace(":", "_")
        self._tokens_path      = os.path.join(token_dir, f"{safe_name}_tokens.json")
        self._client_info_path = os.path.join(token_dir, f"{safe_name}_client_info.json")

    async def get_tokens(self) -> OAuthToken | None:
        return self._load(self._tokens_path, OAuthToken)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._save(self._tokens_path, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._load(self._client_info_path, OAuthClientInformationFull)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._save(self._client_info_path, client_info)

    def _save(self, path: str, model) -> None:
        data = model.model_dump(mode="json")
        # Store issue time so proactive_refresh() can calculate expiry.
        data["_issued_at"] = time.time()
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self, path: str, model_class):
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                raw = json.load(f)
            # Strip our private metadata fields before handing to pydantic.
            clean = {k: v for k, v in raw.items() if not k.startswith("_")}
            return model_class.model_validate(clean)
        except Exception:
            os.remove(path)
            return None


# ---------------------------------------------------------------------------
# Proactive token refresh
# ---------------------------------------------------------------------------

def _token_path(client_name: str) -> str:
    token_dir = os.path.expanduser(os.getenv("TOKEN_STORAGE_DIR", "~/.github_agent_tokens"))
    safe_name = client_name.replace(" ", "_").replace("/", "_").replace(":", "_")
    return os.path.join(token_dir, f"{safe_name}_tokens.json")


def _client_info_path(client_name: str) -> str:
    token_dir = os.path.expanduser(os.getenv("TOKEN_STORAGE_DIR", "~/.github_agent_tokens"))
    safe_name = client_name.replace(" ", "_").replace("/", "_").replace(":", "_")
    return os.path.join(token_dir, f"{safe_name}_client_info.json")


def proactive_refresh(client_name: str) -> bool:
    """
    Check if the stored access token is expired (or close to it) and refresh
    it using the refresh_token grant — without any user interaction.

    Returns True if a refresh was performed, False if the token was still valid
    or if refresh was not possible.  Exceptions are swallowed so callers are
    never blocked.
    """
    tokens_path = _token_path(client_name)
    client_info_p = _client_info_path(client_name)

    if not os.path.exists(tokens_path) or not os.path.exists(client_info_p):
        return False

    try:
        with open(tokens_path) as f:
            token_data = json.load(f)
        with open(client_info_p) as f:
            client_data = json.load(f)

        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return False

        issued_at  = token_data.get("_issued_at", 0.0)
        expires_in = token_data.get("expires_in") or 599
        age        = time.time() - issued_at
        remaining  = expires_in - age

        if remaining > REFRESH_MARGIN:
            # Token still fresh — nothing to do.
            return False

        # Token is expired or close to expiry — refresh it.
        client_id     = client_data.get("client_id", "")
        client_secret = client_data.get("client_secret", "")

        post_data = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
        }).encode()

        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=post_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        new_token = json.loads(resp.read())

        # Persist with our _issued_at marker so the next call can check freshness.
        new_token["_issued_at"] = time.time()
        with open(tokens_path, "w") as f:
            json.dump(new_token, f, indent=2)

        print(f"[auth] Token refreshed (was {age:.0f}s old, expires_in={expires_in}s).")
        return True

    except Exception as exc:
        print(f"[auth] Proactive refresh failed: {exc}")
        return False


def create_oauth_provider(server_url: str, client_name: str) -> OAuthClientProvider:
    # Refresh the stored token before the mcp library checks it, so it finds a
    # valid token and never triggers the interactive PKCE flow.
    proactive_refresh(client_name)

    redirect_uri = os.getenv("REDIRECT_URI", "https://cbg-obot.com/")
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name=client_name,
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="",
        ),
        storage=FileTokenStorage(client_name),
        redirect_handler=handle_redirect,
        callback_handler=handle_callback,
    )
