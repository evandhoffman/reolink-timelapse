import asyncio
import logging
import random
import string
import time

import httpx

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN = 300   # re-login this many seconds before expiry
_LOGIN_RETRIES = 5
_LOGIN_RETRY_DELAY = 30       # seconds between login retries (rspCode -5 = session limit)
_LOGOUT_TIMEOUT = 5.0         # seconds — must be well under docker stop_grace_period


class ReolinkNVR:
    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        self.username = username
        self.password = password
        self._token: str | None = None
        self._token_time: float = 0.0
        self._token_ttl: int = 3600       # overwritten from actual leaseTime on login
        self._login_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._base_url = f"http://{host}/api.cgi"

    # ── Session management ────────────────────────────────────────────────

    async def _logout(self) -> None:
        """
        Explicitly log out the current session so the NVR slot is freed
        immediately.  Uses a short timeout so this always completes within
        the Docker stop_grace_period even if the NVR is slow.
        """
        if not self._token:
            return
        token = self._token
        self._token = None
        self._token_time = 0.0
        try:
            resp = await self._client.post(
                self._base_url,
                params={"cmd": "Logout", "token": token},
                json=[{"cmd": "Logout", "action": 0, "param": {}}],
                timeout=_LOGOUT_TIMEOUT,
            )
            code = resp.json()[0].get("code", -1)
            if code == 0:
                logger.info("NVR session logged out")
            else:
                logger.warning(
                    f"NVR logout returned code {code} — session may linger until it expires"
                )
        except Exception as exc:
            logger.warning(f"NVR logout error (session will expire on its own): {exc}")

    async def _login(self) -> None:
        """
        Log in and store the token.  Always logs out the previous session
        first so we never hold more than one slot on the NVR at a time.
        Retries up to _LOGIN_RETRIES times on rspCode -5 (session limit).
        """
        await self._logout()  # free our slot before taking a new one

        last_data: dict = {}
        for attempt in range(1, _LOGIN_RETRIES + 1):
            if attempt > 1:
                logger.info(
                    f"Login retry {attempt}/{_LOGIN_RETRIES} in {_LOGIN_RETRY_DELAY}s "
                    f"(waiting for NVR session slots to free up) ..."
                )
                await asyncio.sleep(_LOGIN_RETRY_DELAY)

            logger.info(f"Logging in to NVR at {self.host} ...")
            try:
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
            except Exception as exc:
                logger.warning(f"Login HTTP error (attempt {attempt}/{_LOGIN_RETRIES}): {exc}")
                continue

            last_data = resp.json()[0]
            if last_data.get("code", -1) == 0:
                token_data = last_data["value"]["Token"]
                self._token = token_data["name"]
                self._token_ttl = token_data.get("leaseTime", 3600)
                self._token_time = time.monotonic()
                logger.info(
                    f"NVR login successful (token valid for {self._token_ttl}s)"
                )
                return

            rsp_code = last_data.get("error", {}).get("rspCode")
            logger.warning(
                f"Login failed (attempt {attempt}/{_LOGIN_RETRIES}, rspCode={rsp_code})"
            )
            if rsp_code != -5:
                break  # -5 = session limit, worth waiting for; anything else: fail fast

        raise RuntimeError(f"NVR login failed after {_LOGIN_RETRIES} attempts: {last_data}")

    async def _ensure_token(self) -> str:
        """
        Return a valid token, logging in (once, under a lock) only when
        the current token is absent or within _TOKEN_REFRESH_MARGIN of expiry.
        """
        # Fast path — no lock needed
        if self._token is not None:
            age = time.monotonic() - self._token_time
            if age <= (self._token_ttl - _TOKEN_REFRESH_MARGIN):
                return self._token

        # Slow path — serialize so exactly one coroutine logs in
        async with self._login_lock:
            age = time.monotonic() - self._token_time
            if self._token is None or age > (self._token_ttl - _TOKEN_REFRESH_MARGIN):
                await self._login()
            return self._token  # type: ignore[return-value]

    # ── NVR queries ───────────────────────────────────────────────────────

    async def get_online_channels(self) -> list[dict]:
        """
        Return online channels, each enriched with 'name', 'resolution',
        and 'fps'.  Detail requests are sequential to keep NVR load low.
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

        online = [ch for ch in data[0]["value"]["status"] if ch.get("online") == 1]

        # Fetch name + encoding info sequentially — avoids hammering the NVR
        # with a burst of parallel requests on startup
        for ch in online:
            try:
                detail = await self._get_channel_detail(ch["channel"])
                ch.update(detail)
            except Exception as exc:
                logger.warning(f"Channel {ch['channel']}: detail fetch failed: {exc}")
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
        name = (
            resp.json()[0].get("value", {})
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
        main = resp.json()[0].get("value", {}).get("Enc", {}).get("mainStream", {})

        return {
            "name": name,
            "resolution": main.get("size", "?"),
            "fps": main.get("frameRate", "?"),
        }

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
            rsp_code: int | None = None
            detail: str = resp.text[:300]
            try:
                body = resp.json()
                rsp_code = body[0]["error"]["rspCode"]
                detail = body[0]["error"]["detail"]
            except Exception:
                pass
            # Only invalidate the token on actual auth errors; transient NVR
            # errors (-12 "get config failed", etc.) leave the token intact.
            if rsp_code == -6:
                self._token_time = 0.0
            raise RuntimeError(f"Snap rspCode={rsp_code}: {detail}")
        return resp.content

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._logout()
        await self._client.aclose()

    async def __aenter__(self) -> "ReolinkNVR":
        return self

    async def __aexit__(self, *_) -> None:
        await self.aclose()
