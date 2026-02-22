import asyncio
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
        self._login_lock = asyncio.Lock()
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
        # Fast path: token is still valid — no lock needed
        if self._token is not None:
            age = time.monotonic() - self._token_time
            if age <= (_TOKEN_TTL - _TOKEN_REFRESH_MARGIN):
                return self._token

        # Slow path: serialize so only one coroutine actually logs in.
        # All others wait, then return the token the first one fetched.
        async with self._login_lock:
            # Re-check after acquiring the lock — another coroutine may have
            # already refreshed the token while we were waiting.
            age = time.monotonic() - self._token_time
            if self._token is None or age > (_TOKEN_TTL - _TOKEN_REFRESH_MARGIN):
                await self._login()
            return self._token  # type: ignore[return-value]

    async def get_online_channels(self) -> list[dict]:
        """
        Return online channels enriched with 'name', 'resolution', and 'fps'
        fetched from GetOsd / GetEnc per channel.
        """
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

        # Enrich each channel with name + encoding info in parallel
        details = await asyncio.gather(
            *[self._get_channel_detail(ch["channel"]) for ch in online],
            return_exceptions=True,
        )
        for ch, detail in zip(online, details):
            if isinstance(detail, dict):
                ch.update(detail)
            else:
                logger.warning(f"Channel {ch['channel']}: could not fetch detail: {detail}")
                ch.setdefault("name", f"ch{ch['channel']}")

        logger.info(f"Online channels: {[ch['channel'] for ch in online]}")
        return online

    async def _get_channel_detail(self, channel: int) -> dict:
        """Fetch OSD name + main-stream encoding info for one channel."""
        token = await self._ensure_token()
        resp = await self._client.post(
            self._base_url,
            params={"cmd": "GetOsd", "token": token},
            json=[{"cmd": "GetOsd", "action": 0, "param": {"channel": channel}}],
        )
        resp.raise_for_status()
        osd_data = resp.json()
        name = (
            osd_data[0].get("value", {})
            .get("Osd", {})
            .get("osdChannel", {})
            .get("name", f"ch{channel}")
        )

        resp = await self._client.post(
            self._base_url,
            params={"cmd": "GetEnc", "token": token},
            json=[{"cmd": "GetEnc", "action": 0, "param": {"channel": channel}}],
        )
        resp.raise_for_status()
        enc_data = resp.json()
        main = enc_data[0].get("value", {}).get("Enc", {}).get("mainStream", {})
        resolution = main.get("size", "?")
        fps = main.get("frameRate", "?")

        return {"name": name, "resolution": resolution, "fps": fps}

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
            # Parse the error body to distinguish auth failures from transient
            # NVR errors like "get config failed" (rspCode -12).
            rsp_code: int | None = None
            detail: str = resp.text[:300]
            try:
                body = resp.json()
                rsp_code = body[0]["error"]["rspCode"]
                detail = body[0]["error"]["detail"]
            except Exception:
                pass

            # Only invalidate the token for actual auth errors (-6 = bad/expired token).
            # Transient NVR errors (-12, etc.) leave the token intact.
            if rsp_code == -6:
                self._token_time = 0.0

            raise RuntimeError(f"Snap rspCode={rsp_code}: {detail}")
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ReolinkNVR":
        return self

    async def __aexit__(self, *_) -> None:
        await self.aclose()
