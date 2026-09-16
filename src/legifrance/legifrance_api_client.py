import os
import logging
from dotenv import load_dotenv
import httpx

logger = logging.getLogger(__name__)


class BaseLegiFranceClient:
    OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

    def __init__(self):
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
    def __init__(self):
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
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    def search(self, payload: dict) -> httpx.Response:
        return self.session.post(f"{self.API_URL}/search", headers=self.headers, json=payload)

    def download_acco(self, payload: dict) -> httpx.Response:
        return self.session.post(f"{self.API_URL}/consult/acco", headers=self.headers, json=payload)


class AsyncLegiFranceClient(BaseLegiFranceClient):
    def __init__(self):
        super().__init__()
        self.session = None
        self.headers = {}

    async def __aenter__(self):
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
        if self.session:
            await self.session.aclose()

    async def search(self, payload: dict) -> httpx.Response:
        return await self.session.post(f"{self.API_URL}/search", headers=self.headers, json=payload)

    async def download_acco(self, payload: dict) -> httpx.Response:
        return await self.session.post(f"{self.API_URL}/consult/acco", headers=self.headers, json=payload)