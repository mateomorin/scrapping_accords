"""
HTTP clients for the LegiFrance (piste.gouv.fr) API.

Synchronous and asynchronous clients handling OAuth client-credentials
authentication, with the search and ACCO download endpoints.
"""

import logging
import os

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class BaseLegiFranceClient:
    """Base client holding the OAuth client-credentials payload."""

    OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

    def __init__(self):
        """Load the credentials from the environment and build the auth payload."""
        load_dotenv(override=True)
        self.client_id = os.environ["LEGIFRANCE_CLIENT_ID"]
        self.client_secret = os.environ["LEGIFRANCE_CLIENT_SECRET"]
        self._auth_payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "openid",
        }


class LegiFranceClient(BaseLegiFranceClient):
    """Synchronous LegiFrance client (httpx.Client)."""

    def __init__(self):
        """Open the session and request an OAuth access token."""
        super().__init__()
        self.session = httpx.Client()
        # Request session token
        response = self.session.post(
            self.OAUTH_URL,
            data=self._auth_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def __enter__(self):
        """Return the client (context manager support)."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the HTTP session."""
        self.session.close()

    def search(self, payload: dict) -> httpx.Response:
        """POST a search payload to the /search endpoint."""
        return self.session.post(f"{self.API_URL}/search", headers=self.headers, json=payload)

    def download_acco(self, payload: dict) -> httpx.Response:
        """POST a payload to the /consult/acco endpoint and return the document."""
        return self.session.post(f"{self.API_URL}/consult/acco", headers=self.headers, json=payload)


class AsyncLegiFranceClient(BaseLegiFranceClient):
    """Asynchronous LegiFrance client (httpx.AsyncClient)."""

    def __init__(self):
        """Initialize placeholders; the session is created in __aenter__."""
        super().__init__()
        self.session = None
        self.headers = {}

    async def __aenter__(self):
        """Create the async session and request an OAuth access token."""
        self.session = httpx.AsyncClient()
        # Request session token
        response = await self.session.post(
            self.OAUTH_URL,
            data=self._auth_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the async session."""
        if self.session:
            await self.session.aclose()

    async def search(self, payload: dict) -> httpx.Response:
        """POST a search payload to the /search endpoint."""
        return await self.session.post(f"{self.API_URL}/search", headers=self.headers, json=payload)

    async def download_acco(self, payload: dict) -> httpx.Response:
        """POST a payload to the /consult/acco endpoint and return the document."""
        return await self.session.post(
            f"{self.API_URL}/consult/acco", headers=self.headers, json=payload
        )
