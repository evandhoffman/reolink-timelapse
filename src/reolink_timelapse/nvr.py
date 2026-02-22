import asyncio
import logging
import random
import string
import time

import httpx

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN = 300  # re-login this many seconds before expiry
_LOGIN_RETRIES = 5
_LOGIN_RETRY_DELAY = 30  # seconds between login retries (-5 = session limit hit)


class ReolinkNVR:
    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        self.username = username
        self.password = password
        self._token: str | None = None
        self._token_time: float = 0.0
        self._token_ttl: int = 3600       # updated from actual leaseTime on each login
        self._login_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._base_url = f"http://{host}/api.cgi"

    async def _logout(self) -> None:
        """Best-effort logout of the current session to free a slot on the NVR."""
        if not self._token:
            return
        try:
            resp = await self._client.post(
                self._base_url,
                params={"cmd": "Logout", "token": self._token},
                json=[{"cmd": "Logout", "action": 0, "param": {}}],
            )
            code = resp.json()[0].get("code", -1)
            if code == 0:
                logger.info("Logged out of NVR session")
            else:
                logger.warning(f"NVR logout returned code {code} (session may linger)")
        except Exception as exc:
            logger.warning(f"NVR logout error (ignored): {exc}")
        finally:
            self._token = None
            self._token_time = 0.0

    async def _login(self) -> None:
        # Always clean up the existing session first so we don't accumulate
        # stale slots on the NVR (which caps concurrent HTTP sessions).
        await self._logout()

        for attempt in range(1, _LOGIN_RETRIES + 1):
            if attempt > 1:
                logger.info(
                    f"Login retry {attempt}/{_LOGIN_RETRIES} "
                    f"in {_LOGIN_RETRY_DELAY}s (NVR session limit may still be clearing) ..."
                )
                await asyncio.sleep(_LOGIN_RETRY_DELAY)

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

            if data[0].get("code", -1) == 0:
                token_data = data[0]["value"]["Token"]
                self._token = token_data["name"]
                self._token_ttl = token_data.get("leaseTime", 3600)
                self._token_time = time.monotonic()
                logger.info(
                    f"NVR login successful (token valid for {self._token_ttl}s)"
                )
                return

            rsp_code = data[0].get("error", {}).get("rspCode")
            logger.warning(f"Login failed (attempt {attempt}/{_LOGIN_RETRIES}, rspCode={rsp_code})")
            if rsp_code != -5:
                # -5 = session limit; worth retrying.  Any other error: fail fast.
                break

        raise RuntimeError(f"NVR login failed after {_LOGIN_RETRIES} attempts: {data[0]}")

    async def _ensure_token(self) -> str:
        # Fast path: token is still valid — no lock needed
        if self._token is not None:
            age = time.monotonic() - self._token_time
            if age <= (self._token_ttl - _TOKEN_REFRESH_MARGIN):
                return self._token

        # Slow path: serialize so only one coroutine actually logs in.
        # All others wait, then return the token the first one fetched.
        async with self._login_lock:
            # Re-check after acquiring the lock — another coroutine may have
            # already refreshed the token while we were waiting.
            age = time.monotonic() - self._token_time
            if self._token is None or age > (self._token_ttl - _TOKEN_REFRESH_MARGIN):
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
        await self._logout()
        await self._client.aclose()

    async def __aenter__(self) -> "ReolinkNVR":
        return self

    async def __aexit__(self, *_) -> None:
        await self.aclose()
