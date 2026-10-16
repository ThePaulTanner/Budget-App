"""The web page's cookie sign-in, and the rules that keep the cookie from being abused."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import APP_CSP, WEB_COOKIE, create_api_app

from .conftest import PASSWORD

SAME = {"X-Requested-With": "HomeFinance"}
WEB_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "web"


@pytest.fixture
def web(services):
    # https, because the cookie is Secure and the client only sends those over https
    return TestClient(create_api_app(services), base_url="https://testserver")


def sign_in(web, login, **extra):
    return web.post(
        "/web/login",
        json={"username": "paul", "password": PASSWORD, "code": login.code()},
        headers={**SAME, **extra},
    )


def test_login_sets_a_locked_down_cookie_and_hides_the_refresh_token(web, login):
    response = sign_in(web, login)
    assert response.status_code == 200
    body = response.json()
    assert "refresh_token" not in body
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{WEB_COOKIE}=")
    assert WEB_COOKIE.startswith("__Host-")
    for flag in ("HttpOnly", "Secure", "Path=/", "SameSite=strict"):
        assert flag.lower() in cookie.lower(), flag
    assert "domain" not in cookie.lower()
    # the access token works as a normal bearer token
    summary = web.get("/api/summary", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert summary.status_code == 200


def test_the_cookie_alone_does_not_open_the_data_api(web, login):
    assert sign_in(web, login).status_code == 200
    assert web.get("/api/summary").status_code == 401  # cookie is sent, but only a bearer token counts


def test_wrong_password_and_missing_header_and_cross_site_are_refused(web, login):
    bad = web.post("/web/login", json={"username": "paul", "password": "wrong password!!", "code": login.code()}, headers=SAME)
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
    no_header = web.post("/web/login", json={"username": "paul", "password": PASSWORD, "code": login.code()})
    assert no_header.status_code == 403
    for site in ("cross-site", "same-site"):
        assert sign_in(web, login, **{"Sec-Fetch-Site": site}).status_code == 403
    assert sign_in(web, login, **{"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_refresh_rotates_the_cookie(web, login, clock):
    sign_in(web, login)
    first = web.cookies.get(WEB_COOKIE)
    assert web.post("/web/refresh").status_code == 403  # needs the custom header
    response = web.post("/web/refresh", headers=SAME)
    assert response.status_code == 200 and "refresh_token" not in response.json()
    assert web.cookies.get(WEB_COOKIE) != first
    assert web.get("/api/summary", headers={"Authorization": f"Bearer {response.json()['access_token']}"}).status_code == 200


def test_refresh_without_a_cookie_or_with_a_bad_one_is_401(web):
    assert web.post("/web/refresh", headers=SAME).status_code == 401
    web.cookies.set(WEB_COOKIE, "not-a-real-token")
    response = web.post("/web/refresh", headers=SAME)
    assert response.status_code == 401
    assert 'max-age=0' in response.headers["set-cookie"].lower() or "expires" in response.headers["set-cookie"].lower()


def test_an_old_cookie_replayed_after_the_grace_period_revokes_the_device(web, login, clock):
    sign_in(web, login)
    old = web.cookies.get(WEB_COOKIE)
    first = web.post("/web/refresh", headers=SAME).json()
    clock.advance(120)
    web.cookies.set(WEB_COOKIE, old)  # a stolen copy of the previous cookie
    assert web.post("/web/refresh", headers=SAME).status_code == 401
    assert web.get("/api/summary", headers={"Authorization": f"Bearer {first['access_token']}"}).status_code == 401


def test_logout_revokes_the_session_and_clears_the_cookie(web, login):
    token = sign_in(web, login).json()["access_token"]
    assert web.post("/web/logout").status_code == 403
    response = web.post("/web/logout", headers=SAME)
    assert response.status_code == 200
    assert web.get("/api/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert web.post("/web/refresh", headers=SAME).status_code == 401


def test_the_app_page_has_a_strict_csp_and_no_inline_code(web):
    page = web.get("/app")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == APP_CSP
    assert page.headers["cache-control"] == "no-store"
    for needle in ("script-src 'self'", "style-src 'self'", "frame-ancestors 'none'", "default-src 'none'"):
        assert needle in APP_CSP
    assert "unsafe" not in APP_CSP
    html = (WEB_DIR / "index.html").read_text()
    assert "<script>" not in html and " style=" not in html and " onclick=" not in html
    assert web.get("/static/web/app.js").status_code == 200
    assert web.get("/static/web/app.css").status_code == 200


def test_the_script_never_uses_innerhtml_or_other_sites():
    js = (WEB_DIR / "app.js").read_text().replace("http://www.w3.org/2000/svg", "")  # an XML name, not a request
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "sessionStorage"):
        assert bad not in js, bad
    assert "http://" not in js and "https://" not in js
