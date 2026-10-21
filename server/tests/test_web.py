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
    directives = dict(part.strip().split(" ", 1) for part in APP_CSP.split(";") if part.strip())
    # Only our own code, plus Plaid's Link script (fetched only when linking a bank).
    assert directives["script-src"] == "'self' https://cdn.plaid.com"
    assert "unsafe" not in directives["script-src"] and "unsafe" not in APP_CSP.replace("style-src 'self' 'unsafe-inline'", "")
    assert directives["frame-ancestors"] == "'none'" and directives["base-uri"] == "'none'"
    assert directives["frame-src"] == "https://cdn.plaid.com"
    html = (WEB_DIR / "index.html").read_text()
    assert "<script>" not in html and " style=" not in html and " onclick=" not in html
    assert web.get("/static/web/app.js").status_code == 200
    assert web.get("/static/web/app.css").status_code == 200
    assert web.get("/static/web/theme.js").status_code == 200


def test_the_script_never_uses_innerhtml_and_loads_only_plaids_link_script():
    js = (WEB_DIR / "app.js").read_text()
    js = js.replace("http://www.w3.org/2000/svg", "")  # an XML name, not a request
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "sessionStorage"):
        assert bad not in js, bad
    plaid = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"
    assert js.count(plaid) == 1  # one constant, used only when a link button is pressed
    assert "http://" not in js and "https://" not in js.replace(plaid, "")
    html = (WEB_DIR / "index.html").read_text()
    assert "plaid" not in html.lower()  # never loaded at start-up


def test_theme_script_only_keeps_three_harmless_notes():
    js = (WEB_DIR / "theme.js").read_text()
    assert js.count("localStorage") == 2  # one guarded read, one guarded write
    for key in ('"hf-mode"', '"hf-accent"', '"hf-passkey"'):
        assert key in js
    for bad in ("fetch(", "XMLHttpRequest", "innerHTML", "eval(", "http://", "https://", "document.cookie"):
        assert bad not in js, bad
    assert js.count("try {") == 2 and js.count("catch (_)") == 2  # storage can be blocked
    html = (WEB_DIR / "index.html").read_text()
    # loaded from the head, so the saved scheme applies before the page paints
    assert html.index("theme.js") < html.index("<body>")
    css = (WEB_DIR / "app.css").read_text()
    for accent in ("blue", "teal", "green", "purple", "orange", "rose"):
        assert f'[data-accent="{accent}"]' in css


def test_every_page_has_the_logo_as_its_icon(web):
    import re

    for path in ("/app", "/"):
        page = web.get(path)
        assert page.status_code == 200
        hrefs = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', page.text)
        assert "/static/web/favicon.svg" in hrefs and "/static/web/apple-touch-icon.png" in hrefs
        for href in hrefs:
            icon = web.get(href)  # served without signing in, and allowed by the page's own rules
            assert icon.status_code == 200 and len(icon.content) > 100, href
    assert web.get("/static/web/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    assert web.get("/static/web/apple-touch-icon.png").content.startswith(b"\x89PNG")


def test_stand_alone_bank_page_links_to_the_app():
    html = (WEB_DIR.parent / "index.html").read_text()
    assert 'href="/app"' in html  # the bare address must lead somewhere other than the bank list


def test_phone_layout_turns_zoom_off_and_inputs_do_not_trigger_it():
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "app/static/web"
    script, css = (web / "app.js").read_text(), (web / "app.css").read_text()
    assert "(max-width: 760px) and (pointer: coarse)" in script and "user-scalable=no" in script
    assert "touch-action: manipulation" in css and "input, select, textarea { font-size: 16px; }" in css


def test_sign_in_page_has_the_face_id_controls_the_script_relies_on():
    html = (WEB_DIR / "index.html").read_text()
    for needed in ('id="passkey-first"', 'id="passkey-login"', 'id="password-submit"', 'id="password-passkey-submit"', 'id="passkey-setup"'):
        assert needed in html, needed


def test_there_is_one_compact_size_and_no_density_choice():
    for name in ("app.js", "app.css", "theme.js"):
        assert "density" not in (WEB_DIR / name).read_text().lower(), name
    assert "--font: 15px" in (WEB_DIR / "app.css").read_text()


def test_script_understands_that_invalidstateerror_means_a_passkey_already_exists_here():
    js = (WEB_DIR / "app.js").read_text()
    assert 'err.name === "InvalidStateError"' in js and "class PasskeyExists" in js


def test_page_tabs_are_sticky_and_their_heights_are_measured():
    css, js = (WEB_DIR / "app.css").read_text(), (WEB_DIR / "app.js").read_text()
    assert ".subtabs { position: sticky;" in css and ".sticky-tabs { position: sticky;" in css
    assert "--topbar-h" in js and "sticky-tabs" in js


def test_sign_in_page_layout_two_boxes_and_face_id_below_sign_in():
    html = (WEB_DIR / "index.html").read_text()
    assert "Sign in to your private finance dashboard" not in html and "or use your password and code" not in html
    outer, inner = html.index('class="login-outer"'), html.index('class="login-inner"')
    sign_in, face = html.index('id="password-submit"'), html.index('id="passkey-login"')
    assert outer < inner < sign_in < face  # the Face ID button follows the Sign in button, outside the inner box
    assert html.index("</div>", sign_in) < face


def test_sheet_buttons_stay_pinned_below_the_scrolling_part():
    js = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    assert "sheet-body" in js
    assert "dialog#sheet .sheet-body" in css and "overflow-y: auto" in css


def test_reconnect_notice_is_a_count_on_the_cog_not_a_banner():
    html = (WEB_DIR / "index.html").read_text()
    js = (WEB_DIR / "app.js").read_text()
    assert 'id="banner"' not in html and 'id="alert"' not in html and "Reconnect it in Settings" not in js
    assert "renderNotices" in js
    assert 'id="menu-count"' in html and "tab=banks" in js


def test_sign_in_page_does_not_focus_the_username_when_face_id_is_offered():
    js = (WEB_DIR / "app.js").read_text()
    assert 'if (user && $("passkey-first").hidden) user.focus();' in js


def test_phone_positions_get_the_full_row_for_their_name():
    css = (WEB_DIR / "app.css").read_text()
    assert ".row.holding .grow { flex: 1 1 100%; }" in css and 'class: "row click holding"' in (WEB_DIR / "app.js").read_text()


def test_phone_sign_in_fills_the_screen():
    css = (WEB_DIR / "app.css").read_text()
    assert "min-height: 100dvh" in css and ".login-outer { flex: 1 1 auto;" in css and ".login-inner { flex: 1 1 auto;" in css


def test_spending_summary_lives_on_the_spending_page_not_the_overview():
    js = (WEB_DIR / "app.js").read_text()
    overview = js[js.index("async function viewOverview"):js.index("async function viewSpendingSummary")]
    assert "Top categories" not in overview and "Spent in" not in overview
    assert '["summary", "This month", viewSpendingSummary' in js


def test_phone_filters_sit_two_to_a_line_under_the_search_box():
    css = (WEB_DIR / "app.css").read_text()
    assert ".toolbar select { flex-basis: calc(50% - 8px); }" in css


def test_desktop_spending_tabs_and_positions_are_tidy():
    css = (WEB_DIR / "app.css").read_text()
    assert ".subtabs { width: 100%; max-width: 1120px;" in css and ".row.holding .amount { display: flex;" in css


def test_overview_cards_show_what_is_owed_so_rows_add_up_to_the_total():
    js = (WEB_DIR / "app.js").read_text()
    summary = js[js.index("function paymentsSummary"):js.index("// Every account and its balance")]
    assert "money(c.current_balance, c.currency)" in summary and "Balance owed" in summary
    assert "c.statement_balance ?? c.current_balance" not in summary

def test_net_worth_chart_fades_the_estimated_days_and_says_so():
    js = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    assert "r.estimated" in js and "estimates, worked back from your transactions" in js
    assert ".chart .line.estimated" in css


def test_net_worth_ranges_are_1m_3m_ytd_all_and_settings_tabs_use_the_reserved_bar():
    js = (WEB_DIR / "app.js").read_text()
    assert '["YTD", ytdDays]' in js and '["1Y", ' not in js[js.index("NW_RANGES"):js.index("NW_RANGES") + 200]
    settings = js[js.index("function show() {\n      const [groupKey"):]
    assert '$("subtabs")' in settings[:900] and "sticky-tabs" not in settings[:900]


def test_on_a_phone_the_page_choice_lives_in_the_top_bar_not_a_bottom_bar():
    js = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    assert 'document.querySelector(".topbar").append(tabsNav)' in js and "placeTabs()" in js
    phone = css[css.index("@media (max-width: 860px) {\n  .app { display: block; }"):]
    assert ".topbar #tabs" in phone and "inset: auto 0 0 0" not in phone


def test_accounts_on_a_phone_are_one_line_with_only_the_banks_logo():
    css = (WEB_DIR / "app.css").read_text()
    tail = css[css.index("Accounts on a phone: one line per account"):]
    assert ".bank-grouped .bank-cell strong { position: absolute;" in tail and "white-space: nowrap" in tail
    assert 'title: a.institution' in (WEB_DIR / "app.js").read_text()


def test_top_title_is_always_the_app_name_and_overview_cards_have_no_count():
    app = (WEB_DIR / "app.js").read_text()
    html = (WEB_DIR / "index.html").read_text()
    assert '<h1 id="page-title">HomeFinance</h1>' in html
    assert '$("page-title").textContent' not in app
    assert 'data.cards.length === 1 ? "" : "s"' not in app
