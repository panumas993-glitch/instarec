import json
import re
from abc import ABC, abstractmethod
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector

try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession
    from curl_cffi.requests.errors import RequestsError as CffiRequestsError
except ImportError:
    CffiAsyncSession = None
    CffiRequestsError = Exception

from .. import log
from .exceptions import AuthError, InstagramError, UserNotFoundError, UserNotLiveError

TIMEOUT_SECONDS = 10

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "x-ig-app-id": "936619743392459",
    "x-asbd-id": "198387",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PROFILE_URL = "https://www.instagram.com/{username}/"
WEB_PROFILE_INFO_URL = "https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
LIVE_API_URL = "https://www.instagram.com/api/v1/live/web_info/?target_user_id={user_id}"
STORY_FEED_API_URL = "https://www.instagram.com/api/v1/feed/user/{user_id}/story/"


class InstagramClient(ABC):
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy
        self.session: aiohttp.ClientSession | None = None
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None

    def get_headers(self) -> dict[str, str]:
        headers = dict(BASE_HEADERS)
        headers.update(self.headers)
        return headers

    def get_cookies(self) -> dict[str, str]:
        return dict(self.cookies)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = ProxyConnector.from_url(self.proxy) if self.proxy else None
            self.session = aiohttp.ClientSession(connector=connector, headers=self.get_headers())
        return self.session

    async def _fetch_url(self, url: str, headers: dict | None = None) -> tuple[int, str, str]:
        req_headers = self.get_headers()
        if headers:
            req_headers.update(headers)
        cookies = self.get_cookies()

        if CffiAsyncSession is not None:
            try:
                async with CffiAsyncSession(impersonate="chrome") as session:
                    resp = await session.get(
                        url,
                        headers=req_headers,
                        cookies=cookies,
                        proxy=self.proxy,
                        timeout=TIMEOUT_SECONDS,
                    )
                    return resp.status_code, resp.text, str(resp.url)
            except (TimeoutError, CffiRequestsError) as e:
                raise AuthError(f"Network error querying Instagram ({type(e).__name__}): {e}") from e
        else:
            log.API.warning(
                "curl_cffi is not installed; falling back to standard aiohttp. "
                "Instagram may block requests due to TLS fingerprinting. "
                "Install via 'pip install curl_cffi'."
            )
            try:
                session = await self._ensure_session()
                async with session.get(
                    url,
                    headers=req_headers,
                    cookies=cookies,
                    timeout=TIMEOUT_SECONDS,
                ) as resp:
                    text = await resp.text()
                    return resp.status, text, str(resp.url)
            except (aiohttp.ClientError, TimeoutError) as e:
                raise AuthError(f"Network error querying Instagram: {e}") from e

    def _extract_user_id_from_html(self, html_text: str, target_username: str) -> str | None:
        for script_match in re.finditer(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html_text, re.DOTALL
        ):
            script_content = script_match.group(1)
            if '"xig_user_by_username"' in script_content:
                try:
                    data = json.loads(script_content)

                    def find_xig_user(obj):
                        if isinstance(obj, dict):
                            if "xig_user_by_username" in obj and isinstance(obj["xig_user_by_username"], dict):
                                return obj["xig_user_by_username"]
                            for val in obj.values():
                                if found := find_xig_user(val):
                                    return found
                        elif isinstance(obj, list):
                            for item in obj:
                                if found := find_xig_user(item):
                                    return found
                        return None

                    user = find_xig_user(data)
                    if user and (user.get("pk") or user.get("id")):
                        return str(user.get("pk") or user.get("id"))
                except Exception:
                    log.API.debug("Relay JSON script parsing failed.")

        id_match = (
            re.search(r'"profile_id":"(\d+)"', html_text)
            or re.search(r"profilePage_(\d+)", html_text)
            or re.search(r'"user_id":"(\d+)"', html_text)
            or re.search(r'"owner":\s*\{\s*"id":\s*"(\d+)"', html_text)
            or re.search(r"/api/v1/feed/user/(\d+)/", html_text)
        )
        if id_match:
            return id_match.group(1)

        return None

    async def _fetch_user_id_from_web_profile_info(self, username: str) -> str | None:
        url = WEB_PROFILE_INFO_URL.format(username=username)
        headers = {
            "x-asbd-id": "198387",
            "Referer": PROFILE_URL.format(username=username),
        }
        try:
            status_code, text, _ = await self._fetch_url(url, headers=headers)
            if status_code == 200:
                data = json.loads(text)
                user = data.get("data", {}).get("user", {})
                if user and (user.get("id") or user.get("pk")):
                    return str(user.get("id") or user.get("pk"))
        except Exception as e:
            log.API.debug(f"web_profile_info fetch failed: {e}")
        return None

    async def fetch_user_id(self, username: str) -> str:
        clean_user = username.strip()
        if "instagram.com/" in clean_user:
            clean_user = clean_user.rstrip("/").split("instagram.com/")[-1].split("/")[0]
        clean_user = clean_user.lstrip("@").strip().lower()

        if clean_user.isdigit():
            return clean_user

        profile_url = PROFILE_URL.format(username=clean_user)
        log.API.debug(f"Fetching user profile page via curl_cffi: {profile_url}")

        status_code = None
        html_text = ""
        final_url = ""
        try:
            status_code, html_text, final_url = await self._fetch_url(profile_url)
        except Exception as e:
            log.API.debug(f"Profile HTML network request failed for '{clean_user}': {e}")

        if status_code == 404:
            raise UserNotFoundError(f"User '{clean_user}' does not exist.")
        if status_code in (401, 403):
            raise AuthError(f"Authentication failed (Status {status_code}). Your session or cookies may be expired.")
        if status_code == 429:
            raise InstagramError(f"Too many requests (429) for {profile_url}")

        if html_text and (user_id := self._extract_user_id_from_html(html_text, clean_user)):
            log.API.debug(f"Resolved user ID '{user_id}' for '{clean_user}' from profile HTML")
            return user_id

        log.API.debug(f"Attempting web_profile_info fallback for '{clean_user}'")
        if user_id := await self._fetch_user_id_from_web_profile_info(clean_user):
            log.API.debug(f"Resolved user ID '{user_id}' via web_profile_info for '{clean_user}'")
            return user_id

        if "/accounts/login" in final_url or "/login" in final_url:
            raise AuthError(
                "Instagram redirected to the login page. Authentication is required or cookies are expired."
            )

        raise UserNotFoundError(f"User '{clean_user}' does not exist.")

    @abstractmethod
    async def fetch_live_info(self, user_id: str) -> dict[str, Any]:
        pass

    @abstractmethod
    async def fetch_story_feed_info(self, user_id: str) -> dict[str, Any]:
        pass

    async def get_mpd(self, identifier: str) -> str:
        clean_id = identifier.strip()
        if "instagram.com/" in clean_id:
            clean_id = clean_id.rstrip("/").split("instagram.com/")[-1].split("/")[0]
        clean_id = clean_id.lstrip("@").strip()

        if not clean_id.isdigit():
            log.API.debug(f"Resolving user ID for username: '{clean_id}'")
            user_id = await self.fetch_user_id(clean_id)
        else:
            user_id = clean_id

        log.API.info(f"Checking live status for {user_id}...")
        try:
            live_info = await self.fetch_live_info(user_id)
            if mpd_url := live_info.get("dash_abr_playback_url"):
                log.API.info(f"Found MPD for {user_id}: {mpd_url}")
                return mpd_url
        except UserNotFoundError:
            log.API.info("User doesn't seem to be live")

        log.API.info("Checking their story feed in case they are a co-host...")
        story_feed_info = await self.fetch_story_feed_info(user_id)
        if (broadcast := story_feed_info.get("broadcast")) and (mpd_url := broadcast.get("dash_abr_playback_url")):
            host = broadcast.get("broadcast_owner", {})
            host_username = host.get("username", user_id)
            log.API.info(f"Found broadcast via story feed (host: {host_username}): {mpd_url}")
            return mpd_url

        raise UserNotLiveError(f"{identifier} is not currently live.")
