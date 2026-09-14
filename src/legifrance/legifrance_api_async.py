import os
import logging

from dotenv import load_dotenv
import httpx

logger = logging.getLogger(__name__)


class LegiFranceAPIClient:
    OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

    def __init__(self):
        load_dotenv(override=True)
        self.client_id = os.environ["LEGIFRANCE_CLIENT_ID"]
        self.client_secret = os.environ["LEGIFRANCE_CLIENT_SECRET"]
        self.session = None
        self.headers = {}

    async def __aenter__(self):
        self.session = httpx.AsyncClient()

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "openid",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = await self.session.post(
            LegiFranceAPIClient.OAUTH_URL, data=payload, headers=headers
        )
        response.raise_for_status()

        access_token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {access_token}"}
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.aclose()

    async def search(self, payload: dict) -> httpx.Response:
        response = await self.session.post(
            f"{LegiFranceAPIClient.API_URL}/search",
            headers=self.headers,
            json=payload,
        )
        return response

    async def download_acco(self, payload: dict) -> httpx.Response:
        response = await self.session.post(
            f"{LegiFranceAPIClient.API_URL}/consult/acco",
            headers=self.headers,
            json=payload,
        )
        return response
