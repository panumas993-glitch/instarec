import json
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

from .. import log
from .client import LIVE_API_URL, STORY_FEED_API_URL, InstagramClient
from .exceptions import AuthError, InstagramError, UserNotFoundError


class CookieClient(InstagramClient):
    def __init__(self, cookie_file: str, proxy: str | None = None):
        super().__init__(proxy)
        self.cookies = self._load_cookies(cookie_file)
        if csrf_token := self.cookies.get("csrftoken"):
            self.headers["x-csrftoken"] = csrf_token
        else:
            log.API.warning("No 'csrftoken' found in cookies. API calls may fail.")

    def _load_cookies(self, cookie_file: str) -> dict[str, str]:
        if not Path(cookie_file).exists():
            raise FileNotFoundError(f"Cookie file not found: {cookie_file}")

        log.API.debug(f"Loading cookies from: {cookie_file}")
        jar = MozillaCookieJar(cookie_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            log.API.debug("Cookie session built successfully.")
            return {cookie.name: cookie.value for cookie in jar}
        except Exception as e:
            raise AuthError(f"Could not parse cookie file: {e}") from e

    async def _get(self, url: str) -> dict:
        log.API.debug(f"Cookie GET: {url}")
        status_code, text, _ = await self._fetch_url(url)
        if status_code in (401, 403):
            raise AuthError(
                f"Authentication failed (Status {status_code}). Your cookies are likely expired or invalid."
            )
        if status_code == 404:
            raise UserNotFoundError(f"Instagram returned 404 for {url}")
        if status_code == 429:
            raise InstagramError(f"Too many requests (429) for {url}")

        if text.lstrip().startswith("<!DOCTYPE") or "<html" in text[:100].lower():
            log.API.debug(f"Raw response (status {status_code}): {text[:500]!r}")
            raise AuthError("Instagram redirected to the login page. Your cookies are invalid.")

        try:
            return json.loads(text)
        except ValueError as e:
            log.API.debug(f"Raw response (status {status_code}): {text[:500]!r}")
            raise AuthError(f"Invalid API response (not JSON): {e}") from e

    async def fetch_live_info(self, user_id: str) -> dict[str, Any]:
        return await self._get(LIVE_API_URL.format(user_id=user_id))

    async def fetch_story_feed_info(self, user_id: str) -> dict[str, Any]:
        return await self._get(STORY_FEED_API_URL.format(user_id=user_id))
