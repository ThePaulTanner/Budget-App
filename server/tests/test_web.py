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
    assert ".subtabs { position: sticky;" in css
    assert "--topbar-h" in js and "measureBars" in js


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


def test_a_position_row_keeps_its_name_beside_the_ticker_and_wraps_when_needed():
    css = (WEB_DIR / "app.css").read_text()
    assert ".hold-head { display: flex; flex-wrap: wrap;" in css and 'class: "row click holding"' in (WEB_DIR / "app.js").read_text()


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
    assert ".subtabs { width: 100%; max-width: 1120px;" in css and ".hold-line {" in css


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


def test_banks_are_logos_alone_on_accounts():
    app = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    assert ".money-table .bank-cell strong, .bank-grouped .bank-cell strong { position: absolute;" in css
    assert 'class: "bank-cell", title: a.institution' in app


def test_merchants_tabs_live_in_the_fixed_bar_not_the_scrolling_page():
    app = (WEB_DIR / "app.js").read_text()
    assert 'bar.append(el("div", { class: "toolbar merch-tabs" }, tabs))' in app
    assert 'el("div", { class: "toolbar sticky-tabs" }, tabs)' not in app


def test_sub_tab_bars_show_no_scroll_bar():
    css = (WEB_DIR / "app.css").read_text()
    assert "#subtabs .seg.wide, #subtabs .tabs-sub { scrollbar-width: none; overflow-x: auto; }" in css
    assert "#subtabs .seg.wide::-webkit-scrollbar, #subtabs .tabs-sub::-webkit-scrollbar { display: none; }" in css


def test_settings_banks_lists_accounts_as_a_table_with_owner_and_hide_columns():
    app = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    start = app.index("function accountsTable")
    table = app[start:app.index("// One block per bank", start)]
    assert 'class: "bank-accounts"' in table and 'class: "col-owners"' in table and 'class: "col-hide"' in table
    assert 'role: "switch"' in table and "accountRow" not in app
    assert ".switch input:checked + .track" in css


def test_page_is_compressed_and_its_assets_are_cached_by_release(web):
    from app import version
    page = web.get("/app", headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    for name in ("theme.js", "app.js", "app.css"):
        assert f"/static/web/{name}?v={version.VERSION}" in page.text
    script = web.get(f"/static/web/app.js?v={version.VERSION}", headers={"Accept-Encoding": "gzip"})
    assert script.headers["content-encoding"] == "gzip"
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert web.get("/static/web/app.js").headers["cache-control"] == "no-cache"


def test_account_sections_do_not_show_an_account_count():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function accountSection")
    section = app[start:app.index("async function viewAccounts", start)]
    assert 'rows.length === 1 ? "" : "s"' not in section


def test_overview_has_no_credit_card_section():
    app = (WEB_DIR / "app.js").read_text()
    overview = app[app.index("async function viewOverview"):]
    overview = overview[:overview.index("\n  }\n")]
    assert "paymentsSummary" not in app and "/api/payments-due" not in overview


def test_account_section_total_is_a_small_line_below_the_table():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function accountSection")
    section = app[start:app.index("async function viewAccounts", start)]
    assert section.index('class: "acct-table bank-grouped"') < section.index('class: "section-total muted"')
    assert "`Total: ${sectionTotals(rows, kind)}`" in section


def test_page_shows_its_release_under_the_name_and_notices_a_newer_server(web):
    from app import version
    page = web.get("/app")
    assert f'<meta name="release" content="{version.VERSION}">' in page.text
    html = (WEB_DIR / "index.html").read_text()
    assert html.index('id="page-title"') < html.index('id="release"') < html.index('id="page-sub"')
    app = (WEB_DIR / "app.js").read_text()
    assert "serverRelease !== pageRelease" in app and "location.reload()" in app
    assert (WEB_DIR / "app.css").read_text().count(".release {") == 1


def test_accounts_do_not_show_value_or_balance_column_headings():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function accountSection")
    section = app[start:app.index("async function viewAccounts", start)]
    assert '"num col-balance", !(credit || kind === "loan")' in section
    assert 'unlabelled ? el("span", { class: "sr", text }) : text' in section


def test_accounts_show_the_owner_as_a_small_line_under_the_name_not_a_column():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function accountSection")
    section = app[start:app.index("async function viewAccounts", start)]
    assert 'class: "muted acct-owner"' in section and 'class: "col-owner"' not in section
    assert '["Owner", "col-owner"]' not in section and "function ownersCell" not in app
    assert ".acct-owner { font-size: .74rem;" in (WEB_DIR / "app.css").read_text()


def test_accounts_page_does_not_show_the_account_number():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function accountSection")
    section = app[start:app.index("async function viewAccounts", start)]
    assert "a.mask" not in section


def test_there_is_no_money_page():
    app = (WEB_DIR / "app.js").read_text()
    html = (WEB_DIR / "index.html").read_text()
    assert "viewMoney" not in app and "DUE_PILLS" not in app and "#/money" not in html
    assert 'data-tab="money"' not in html and "money: [" not in app


def test_cash_flow_lives_on_the_overview_not_in_a_spending_tab():
    app = (WEB_DIR / "app.js").read_text()
    tabs = app[app.index("const SPENDING_TABS"):app.index("let spendingTab")]
    assert "viewCashflow" not in tabs and "Cash flow" not in tabs
    overview = app[app.index("async function viewOverview"):]
    overview = overview[:overview.index("\n  }\n")]
    assert "viewCashflow(id, params, cashflowBox)" in overview
    assert "history.replaceState(null, \"\", `#/overview?${q}`)" in app


def test_budgets_has_its_own_top_level_tab():
    app = (WEB_DIR / "app.js").read_text()
    html = (WEB_DIR / "index.html").read_text()
    tabs = app[app.index("const SPENDING_TABS"):app.index("let spendingTab")]
    assert "viewBudgets" not in tabs
    assert "budgets: viewBudgets" in app and 'budgets: ["Budgets"' in app
    assert 'href="#/budgets" data-tab="budgets"' in html and "tab=budgets" not in app


def test_old_budgets_and_cash_flow_tab_addresses_go_to_their_new_pages():
    app = (WEB_DIR / "app.js").read_text()
    assert 'params.get("tab") === "budgets") name = "budgets"' in app
    assert 'params.get("tab") === "cashflow") { name = "overview"; params.delete("tab"); }' in app


def test_position_rows_are_ticker_and_name_then_held_and_price_then_the_money():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index('class: "hold-head"')
    row = app[start:app.index("if (!activityCard", start)]
    assert row.index("hold-name") < row.index("held") < row.index("avg ") < row.index("now ")
    assert row.index("now ") < row.index("`Book ") < row.index("`Value ") < row.index("P&L ")


def test_a_closed_market_position_is_tagged_closed_with_its_last_trade_time():
    app = (WEB_DIR / "app.js").read_text()
    assert 'h.market_open ? "Live" : "Closed"' in app and "Last trade ${" in app
    assert 'h.live && !h.market_open && lastTrade && el("div", { class: "hold-line hold-note", text: lastTrade })' in app  # visible on a phone, not just a tooltip


def test_the_sign_in_button_says_passkey_not_face_id():
    html = (WEB_DIR / "index.html").read_text()
    app = (WEB_DIR / "app.js").read_text()
    assert ">Sign in with passkey<" in html and ">Sign in and set up a passkey<" in html
    for text in ("Sign in with Face ID", "Set up Face ID?", "Face ID is set up", '"Passkeys (Face ID)"'):
        assert text not in html + app, text


def test_cash_flow_controls_sit_at_the_top_of_the_chart_box_and_are_small():
    app = (WEB_DIR / "app.js").read_text()
    assert 'const parts = [tiles, chartCard, listCard];' in app
    box = app[app.index('const chartCard = el("section"'):]
    assert box.index("controls, customRow") < box.index('class: "card-head"')
    assert '"seg wide compact"' in app and '"seg compact", role: "group", "aria-label": "Group by"' in app
    assert ".cf-controls button.cf-filters {" in (WEB_DIR / "app.css").read_text()


def test_spending_by_category_folds_sub_categories_and_starts_folded():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index('const group = el("div", { class: "sub-group", hidden: true }')
    block = app[start:app.index("const trendCard", start)]
    assert '"aria-expanded": "false"' in block and "group.hidden = !group.hidden" in block
    assert "e.stopPropagation();  // the row itself opens the transactions" in block
    assert ".sub-group[hidden] { display: none; }" in (WEB_DIR / "app.css").read_text()


def test_budgets_sit_on_the_spending_by_category_rows_with_a_red_line_when_over():
    app = (WEB_DIR / "app.js").read_text()
    css = (WEB_DIR / "app.css").read_text()
    page = app[app.index("async function viewBudgets"):app.index("// ---------------------------------------------------------------- Cash flow")]
    assert 'el("h2", { text: "Budgets" })' not in page and "budgetCard" not in page
    assert 'text: "Add budget"' in page and page.index('text: "Add budget"') < page.index('"Spending by category"')
    assert "budgetBar(c.spent, b.monthly_limit)" in page and "budgetBar(spent, sb.monthly_limit)" in page
    assert "const scale = Math.max(limit, spent, 0.01);" in app and 'className = "limit-line"' in app
    assert ".progress.budgeted .limit-line" in css and "var(--red) var(--limit)" in css


def test_settings_banks_start_folded_with_an_arrow_to_open_their_accounts():
    app = (WEB_DIR / "app.js").read_text()
    start = app.index("function bankBlock")
    block = app[start:app.index("// Removing a bank is hidden until you press Edit", start)]
    assert 'class: "bank-body", hidden: true' in block and "foldButton(details," in block
    assert block.index('class: "bank-body"') < block.index("accountsTable(group)")
    assert ".bank-body[hidden] { display: none; }" in (WEB_DIR / "app.css").read_text()


def test_unfolded_banks_stay_unfolded_when_the_list_reloads_and_other_accounts_fold_too():
    app = (WEB_DIR / "app.js").read_text()
    assert "const openBanks = new Set();" in app and "body.hidden = !openBanks.has(key);" in app
    assert 'foldButton(details, `the accounts at ${name}`, i.id)' in app and 'foldButton(details, "the other accounts", "other")' in app


def test_settings_banks_no_longer_has_the_long_explanation_paragraph():
    app = (WEB_DIR / "app.js").read_text()
    assert "Each bank lists its accounts" not in app and "switch hide on" not in app.lower()
    assert "add the people in your household on the People tab first" in app


def test_header_keeps_title_and_controls_on_one_line_and_smaller():
    css = (WEB_DIR / "app.css").read_text()
    assert ".top-actions { flex: none; flex-wrap: nowrap;" in css
    assert ".top-actions .icon-btn { width: 32px; height: 32px; }" in css
    assert ".topbar .titles { min-width: 0; flex: 1 1 0; }" in css


def test_investments_no_longer_shows_the_live_price_and_changes_assume_notes():
    app = (WEB_DIR / "app.js").read_text()
    assert "Prices refresh about every two minutes" not in app
    assert "Changes assume you held today" not in app
    assert "Live prices are turned off on the server" in app


def test_header_controls_shrink_on_very_narrow_phones_instead_of_wrapping():
    css = (WEB_DIR / "app.css").read_text()
    assert ".topbar .titles { min-width: 5.5rem; }" in css
    assert "@media (max-width: 360px)" in css and ".top-actions .viewer select { max-width: 5.25rem;" in css


def test_budgets_month_switcher_and_add_budget_stay_pinned_while_scrolling():
    css = (WEB_DIR / "app.css").read_text()
    assert ".monthnav { position: sticky; top: var(--topbar-h, 64px);" in css


def test_account_sections_have_a_slim_coloured_header_band():
    css = (WEB_DIR / "app.css").read_text()
    assert ".acct-section > .section-head { gap: 8px; margin: calc(var(--pad) * -1) calc(var(--pad) * -1) 4px;" in css
    assert ".acct-section.credit { --sec: var(--c-liab); }" in css and "color-mix(in srgb, var(--sec) 16%, var(--surface))" in css


def test_pinned_month_switcher_stays_on_one_line_on_very_narrow_phones():
    css = (WEB_DIR / "app.css").read_text()
    assert ".monthnav strong { font-size: .85rem; white-space: nowrap; }" in css
    assert ".monthnav strong { overflow: hidden; text-overflow: ellipsis; }" in css


def test_categories_without_a_budget_get_a_plus_target_button_that_asks_for_the_amount():
    app = (WEB_DIR / "app.js").read_text()
    page = app[app.index("async function viewBudgets"):app.index("// ---------------------------------------------------------------- Cash flow")]
    assert 'class: "secondary budget-add"' in page and 'iconSvg("i-target", 16)' in page
    assert "budgetDialog(null, category)" in page and "function budgetDialog(existing, preset = null)" in page
    assert "editButton(b, c.category)" in page and "editButton(sb, code)" in page
    assert 'text: "Add budget"' in page   # the button at the top stays
