import os

from dotenv import load_dotenv
import requests


class LegiFranceAPIClient:
    OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

    def __init__(self):
        load_dotenv(override=True)
        self.client_id = os.environ["LEGIFRANCE_CLIENT_ID"]
        self.client_secret = os.environ["LEGIFRANCE_CLIENT_SECRET"]

        # Create access token
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "openid"
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(
            url=LegiFranceAPIClient.OAUTH_URL,
            data=payload,
            headers=headers
        )

        self.access_token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.access_token}"}

    def search(self, payload):
        response = requests.post(
            url=LegiFranceAPIClient.API_URL + "/search",
            headers=self.headers,
            json=payload
        )

        return response

    def download_acco(self, payload):
        response = requests.post(
            url=LegiFranceAPIClient.API_URL + "/consult/acco",
            headers=self.headers,
            json=payload
        )

        return response
