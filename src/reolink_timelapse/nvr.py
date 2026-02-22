import logging
import random
import string
import time

import httpx

logger = logging.getLogger(__name__)

# Reolink tokens expire after ~1 hour; refresh 5 min before expiry
_TOKEN_TTL = 3600
_TOKEN_REFRESH_MARGIN = 300


class ReolinkNVR:
    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        self.username = username
        self.password = password
        self._token: str | None = None
        self._token_time: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0)
        self._base_url = f"http://{host}/api.cgi"

    async def _login(self) -> None:
        logger.info(f"Logging in to NVR at {self.host} ...")
        resp = await self._client.post(
            self._base_url,
            params={"cmd": "Login"},
            json=[
                {
                    "cmd": "Login",
                    "action": 0,
                    "param": {
                        "User": {
                            "Version": "0",
                            "userName": self.username,
                            "password": self.password,
                        }
                    },
                }
            ],
        )
        resp.raise_for_status()
        data = resp.json()
        if data[0].get("code", -1) != 0:
            raise RuntimeError(f"NVR login failed: {data[0]}")
        self._token = data[0]["value"]["Token"]["name"]
        self._token_time = time.monotonic()
        logger.info("NVR login successful")

    async def _ensure_token(self) -> str:
        age = time.monotonic() - self._token_time
        if self._token is None or age > (_TOKEN_TTL - _TOKEN_REFRESH_MARGIN):
            await self._login()
        return self._token  # type: ignore[return-value]

    async def get_online_channels(self) -> list[dict]:
        token = await self._ensure_token()
        resp = await self._client.post(
            self._base_url,
            params={"cmd": "GetChannelstatus", "token": token},
            json=[{"cmd": "GetChannelstatus", "action": 0, "param": {}}],
        )
        resp.raise_for_status()
        data = resp.json()
        if data[0].get("code", -1) != 0:
            raise RuntimeError(f"GetChannelstatus failed: {data[0]}")
        all_channels = data[0]["value"]["status"]
        online = [ch for ch in all_channels if ch.get("online") == 1]
        logger.info(f"Online channels: {[ch['channel'] for ch in online]}")
        return online

    async def capture_snapshot(self, channel: int) -> bytes:
        token = await self._ensure_token()
        rs = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        resp = await self._client.get(
            self._base_url,
            params={"cmd": "Snap", "channel": channel, "rs": rs, "token": token},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type:
            # Could be an auth error or unsupported channel — force re-login next time
            self._token = None
            raise RuntimeError(
                f"Unexpected content-type '{content_type}': {resp.text[:200]}"
            )
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ReolinkNVR":
        return self

    async def __aexit__(self, *_) -> None:
        await self.aclose()
