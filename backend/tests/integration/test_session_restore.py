"""A session survives a browser restart, within its server-side limits (security-architecture
§3.5). The browser keeps the two session cookies because they carry Max-Age; "restarting" is a
new client holding only those cookies. The server stays authoritative: idle and absolute
timeouts, sign-out, revocation and disabling end the session whatever the browser kept."""

from datetime import timedelta

import httpx

from app.platform.authz.roles import RoleKey
from tests.api_support import PASSWORD, Account, Api, refresh_cookie

ACCESS = "__Host-sv_at"
REFRESH_COOKIE = "__Secure-sv_rt"
REFRESH = "/api/v1/auth/refresh"


def kept_cookies(client: httpx.AsyncClient) -> tuple[str, str]:
    return client.cookies.get(ACCESS) or "", refresh_cookie(client)


def reopen(browser: httpx.AsyncClient, cookies: tuple[str, str]) -> None:
    """The cookies a restarted browser still holds."""
    access, refresh = cookies
    browser.cookies.set(ACCESS, access, path="/")
    browser.cookies.set(REFRESH_COOKIE, refresh, path=REFRESH)


async def signed_in(api: Api, account: Account) -> tuple[str, str]:
    async with api.client() as client:
        response = await api.sign_in(client, account)
        set_cookies = response.headers.get_list("set-cookie")
        # Persistent, not session cookies: both carry a lifetime and the security attributes.
        for name in (ACCESS, REFRESH_COOKIE):
            [cookie] = [value for value in set_cookies if value.startswith(f"{name}=")]
            assert "Max-Age=" in cookie
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
        return kept_cookies(client)


async def test_a_restarted_browser_resumes_a_valid_session_without_a_password_or_code(api: Api) -> None:
    account = await api.create_account()
    cookies = await signed_in(api, account)
    async with api.client() as browser:
        reopen(browser, cookies)
        assert (await browser.get("/api/v1/me")).status_code == 200
    # Later, with the 15-minute access token expired but the session not idle for 30 minutes:
    # the client refreshes once and carries on.
    api.clock.advance(timedelta(minutes=20))
    async with api.client() as browser:
        reopen(browser, cookies)
        assert (await browser.get("/api/v1/me")).status_code == 401
        assert (await browser.post(REFRESH)).status_code == 200
        assert (await browser.get("/api/v1/me")).status_code == 200


async def test_an_idle_session_is_not_restored(api: Api) -> None:
    account = await api.create_account()
    cookies = await signed_in(api, account)
    api.clock.advance(timedelta(minutes=31))
    async with api.client() as browser:
        reopen(browser, cookies)
        assert (await browser.post(REFRESH)).status_code == 401
        assert (await browser.get("/api/v1/me")).status_code == 401


async def test_signing_out_ends_the_session_for_the_cookies_a_browser_kept(api: Api) -> None:
    account = await api.create_account()
    async with api.client() as client:
        await api.sign_in(client, account)
        cookies = kept_cookies(client)
        assert (await client.post("/api/v1/auth/logout")).status_code == 204
        assert client.cookies.get(ACCESS) is None
    async with api.client() as browser:
        reopen(browser, cookies)
        assert (await browser.get("/api/v1/me")).status_code == 401
        assert (await browser.post(REFRESH)).status_code == 401
        # Signing in again needs the password and then the code.
        login = await browser.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert login.status_code == 200
        assert "mfa_token" in login.json()
        assert (await browser.get("/api/v1/me")).status_code == 401


async def test_another_browser_without_the_cookies_must_sign_in(api: Api) -> None:
    account = await api.create_account()
    await signed_in(api, account)
    async with api.client() as other:
        assert (await other.get("/api/v1/me")).status_code == 401
        assert (await other.post(REFRESH)).status_code == 401


async def test_a_disabled_or_offboarded_account_cannot_resume(api: Api) -> None:
    account = await api.create_account()
    cookies = await signed_in(api, account)
    admin = await api.create_account(roles=(RoleKey.SYSTEM_ADMIN.value,))
    async with api.client() as client:
        await api.sign_in(client, admin)
        await api.step_up(client, admin)
        assert (await client.post(f"/api/v1/users/{account.user_id}/disable")).status_code == 200
    async with api.client() as browser:
        reopen(browser, cookies)
        assert (await browser.get("/api/v1/me")).status_code == 401
        assert (await browser.post(REFRESH)).status_code == 401


async def test_resuming_keeps_the_csrf_check_on_refresh(api: Api) -> None:
    account = await api.create_account()
    cookies = await signed_in(api, account)
    async with api.client(csrf=False) as browser:
        reopen(browser, cookies)
        refused = await browser.post(REFRESH)
        assert refused.status_code == 403
        assert refused.json()["code"] == "csrf"
