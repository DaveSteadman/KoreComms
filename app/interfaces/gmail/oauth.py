"""Gmail OAuth helpers used by the WebUI."""
from __future__ import annotations

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def build_auth_url(client_id: str, client_secret: str, redirect_uri: str, state: str) -> str:
    """Return a Google OAuth2 consent URL for Gmail access."""
    from google_auth_oauthlib.flow import Flow  # type: ignore[import-untyped]

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> str:
    """Exchange an auth code for a refresh token and return it."""
    from google_auth_oauthlib.flow import Flow  # type: ignore[import-untyped]

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    return flow.credentials.refresh_token or ""