// HomeFinance web app. Plain JavaScript, no libraries, nothing loaded from other sites.
// The refresh token lives in an HttpOnly cookie this script cannot read; the short-lived
// access token below exists only in this page's memory. All text is inserted with
// textContent (never as HTML), so server data cannot inject markup.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const UNREACHABLE = "Can't reach your server — is Tailscale connected?";
  const MINUS = "−";

  // ---------------------------------------------------------------- helpers

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "value" || key === "checked" || key === "disabled" || key === "hidden" || key === "selected") node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  function svg(tag, attrs, ...kids) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    for (const kid of kids.flat(Infinity)) if (kid) node.append(kid);
    return node;
  }

  const formatters = {};
  function money(value, currency, options) {
    const key = currency + JSON.stringify(options || {});
    formatters[key] ??= new Intl.NumberFormat("en-CA", { style: "currency", currency, ...options });
    return formatters[key].format(value);
  }

  // Plaid's sign: positive amount = money left the account. Spending shows as -$x in the
  // normal colour; income and refunds as +$x in green.
  function signed(amount, currency) {
    if (amount > 0) return { text: MINUS + money(amount, currency), cls: "" };
    if (amount < 0) return { text: "+" + money(-amount, currency), cls: "income" };
    return { text: money(0, currency), cls: "" };
  }

  // "YYYY-MM-DD" is a calendar date, not a moment: build it in local time so it never shifts a day.
  function parseDay(text) {
    const [y, m, d] = text.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function dayLabel(text) {
    const date = parseDay(text);
    const options = { weekday: "long", month: "long", day: "numeric" };
    if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
    return date.toLocaleDateString("en-CA", options);
  }
  function monthLabel(month) {
    const [y, m] = month.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
  }
  function shortMonth(month) {
    const [y, m] = month.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("en-CA", { month: "short" });
  }
  function addMonths(month, delta) {
    const [y, m] = month.split("-").map(Number);
    const index = y * 12 + (m - 1) + delta;
    return `${String(Math.floor(index / 12)).padStart(4, "0")}-${String((index % 12) + 1).padStart(2, "0")}`;
  }
  function ago(timestamp) {
    if (!timestamp) return "never";
    const seconds = Math.round((new Date(timestamp).getTime() - Date.now()) / 1000);
    const rtf = new Intl.RelativeTimeFormat("en-CA", { numeric: "auto" });
    for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
    }
    return "just now";
  }

  const ICONS = {
    INCOME: "\u{1F4B5}", TRANSFER_IN: "\u{2B07}\u{FE0F}", TRANSFER_OUT: "\u{2B06}\u{FE0F}", LOAN_PAYMENTS: "\u{1F3E0}",
    BANK_FEES: "\u{1F3E6}", ENTERTAINMENT: "\u{1F3AC}", FOOD_AND_DRINK: "\u{1F37D}\u{FE0F}", GENERAL_MERCHANDISE: "\u{1F6CD}\u{FE0F}",
    HOME_IMPROVEMENT: "\u{1F528}", MEDICAL: "\u{1FA7A}", PERSONAL_CARE: "\u{1F9F4}", GENERAL_SERVICES: "\u{1F6E0}\u{FE0F}",
    GOVERNMENT_AND_NON_PROFIT: "\u{1F3DB}\u{FE0F}", TRANSPORTATION: "\u{1F697}", TRAVEL: "\u{2708}\u{FE0F}",
    RENT_AND_UTILITIES: "\u{1F4A1}", OTHER: "\u{1F4C1}", GROCERIES: "\u{1F6D2}",
  };
  const KNOWN_CATEGORIES = Object.keys(ICONS);
  // Plaid's top-level categories. A sub-category's code starts with its parent's, as in
  // FOOD_AND_DRINK_FAST_FOOD, so the parent can be found from the code alone.
  const PRIMARIES = KNOWN_CATEGORIES.filter((c) => c !== "GROCERIES");
  function parentOf(code) {
    let best = null;
    for (const p of PRIMARIES) if (code && code.startsWith(`${p}_`) && (!best || p.length > best.length)) best = p;
    return best;
  }
  const icon = (category) => ICONS[category] || ICONS[parentOf(category)] || "\u{1F4C1}";
  function titleCase(code) {
    return code.toLowerCase().split("_").map((w) => (w === "and" ? "&" : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
  }
  // "Food & Drink", or "Food & Drink \u203A Fast Food" for a sub-category.
  function categoryLabel(code) {
    if (!code) return "Uncategorized";
    const parent = parentOf(code);
    return parent ? `${titleCase(parent)} \u203A ${titleCase(code.slice(parent.length + 1))}` : titleCase(code);
  }
  const subLabel = (code) => titleCase(code.slice(parentOf(code).length + 1));
  const TYPE_GROUPS = [["depository", "Cash"], ["credit", "Credit"], ["investment", "Investments"], ["loan", "Loans"], ["other", "Other"]];

  function toast(text, isError = false) {
    const node = $("toast");
    node.textContent = text;
    node.className = isError ? "error" : "";
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, isError ? 7000 : 3500);
  }

  // ---------------------------------------------------------------- talking to the server

  let token = null;
  let tokenExpiry = 0;
  let renewing = null;
  class SignedOut extends Error {}
  class HttpError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function plainFetch(path, options) {
    try {
      return await fetch(path, options);
    } catch (_) {
      throw new Error(UNREACHABLE);
    }
  }

  function takeTokens(data) {
    token = data.access_token;
    tokenExpiry = Date.now() + Math.max(data.expires_in - 30, 5) * 1000;
  }

  async function renew() {
    const response = await plainFetch("/web/refresh", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } });
    if (response.status === 401) { token = null; return false; }
    if (!response.ok) throw new Error(`Could not sign in (${response.status})`);
    takeTokens(await response.json());
    return true;
  }
  function renewOnce() {
    renewing ??= renew().finally(() => { renewing = null; });
    return renewing;
  }

  async function api(path, { method = "GET", body, timeout = 30000 } = {}) {
    if (!token || Date.now() >= tokenExpiry) {
      if (!(await renewOnce())) throw new SignedOut();
    }
    const send = () => plainFetch(path, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeout),
    });
    let response = await send();
    if (response.status === 401) {
      if (!(await renewOnce())) throw new SignedOut();
      response = await send();
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new HttpError(response.status, typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
    return data;
  }

  // ---------------------------------------------------------------- charts

  function lineChart(points, currency) {
    const wrap = el("div");
    if (points.length < 2) return el("p", { class: "empty", text: "Not enough history yet. The chart fills in as days of data are collected." });
    const W = 640, H = 230, L = 62, R = 12, T = 12, B = 26;
    const values = points.map((p) => p.value);
    let lo = Math.min(...values), hi = Math.max(...values);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const x = (i) => L + (i / (points.length - 1)) * (W - L - R);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Net worth over time" });
    for (let i = 0; i <= 3; i++) {
      const v = lo + ((hi - lo) * i) / 3;
      chart.append(svg("line", { class: "gl", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      const label = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" });
      label.textContent = money(v, currency, { notation: "compact", maximumFractionDigits: 1 });
      chart.append(label);
    }
    const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
    chart.append(svg("path", { class: "area", d: `${path} L${x(points.length - 1)},${H - B} L${L},${H - B} Z` }));
    chart.append(svg("path", { class: "line", d: path }));
    for (const i of [0, Math.floor((points.length - 1) / 2), points.length - 1]) {
      const label = svg("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === points.length - 1 ? "end" : "middle" });
      label.textContent = parseDay(points[i].date).toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "2-digit" });
      chart.append(label);
    }
    const marker = svg("circle", { r: 4, class: "bar", cx: x(points.length - 1), cy: y(points[points.length - 1].value) });
    chart.append(marker);
    const caption = el("p", { class: "muted" });
    const show = (i) => {
      marker.setAttribute("cx", x(i));
      marker.setAttribute("cy", y(points[i].value));
      caption.textContent = `${dayLabel(points[i].date)}: ${money(points[i].value, currency)}`;
    };
    show(points.length - 1);
    chart.addEventListener("pointermove", (event) => {
      const box = chart.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W;
      show(Math.max(0, Math.min(points.length - 1, Math.round(((px - L) / (W - L - R)) * (points.length - 1)))));
    });
    chart.addEventListener("pointerleave", () => show(points.length - 1));
    wrap.append(chart, caption);
    return wrap;
  }

  function barChart(rows, currency) {
    const W = 640, H = 200, L = 62, R = 8, T = 10, B = 24;
    const max = Math.max(...rows.map((r) => r.total), 1);
    const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Spending by month" });
    for (let i = 0; i <= 2; i++) {
      const v = (max * i) / 2;
      const yy = T + (1 - v / max) * (H - T - B);
      chart.append(svg("line", { class: "gl", x1: L, x2: W - R, y1: yy, y2: yy }));
      const label = svg("text", { x: L - 6, y: yy + 4, "text-anchor": "end" });
      label.textContent = money(v, currency, { notation: "compact", maximumFractionDigits: 1 });
      chart.append(label);
    }
    const slot = (W - L - R) / rows.length;
    rows.forEach((row, i) => {
      const h = Math.max(0, (row.total / max) * (H - T - B));
      const bar = svg("rect", { class: "bar", x: L + i * slot + slot * 0.18, y: H - B - h, width: slot * 0.64, height: h, rx: 3 });
      const tip = svg("title");
      tip.textContent = `${monthLabel(row.month)}: ${money(row.total, currency)}`;
      bar.append(tip);
      chart.append(bar);
      const label = svg("text", { x: L + i * slot + slot / 2, y: H - 7, "text-anchor": "middle" });
      label.textContent = shortMonth(row.month);
      chart.append(label);
    });
    return chart;
  }

  function progress(percent, plain = false) {
    const cls = plain ? "progress plain" : percent >= 100 ? "progress over" : percent >= 80 ? "progress warn" : "progress";
    return el("div", { class: cls, role: "progressbar", "aria-valuenow": Math.round(percent), "aria-valuemin": 0, "aria-valuemax": 100 },
      (() => { const bar = document.createElement("i"); bar.style.width = `${Math.min(100, Math.max(0, percent))}%`; return bar; })());
  }

  // ---------------------------------------------------------------- shared data

  let baseCurrency = "CAD";
  let navId = 0;
  const view = () => $("view");

  // The household's people, and whose money is on screen (null = the whole household, where every
  // account counts once however many owners it has).
  let people = [];
  let viewer = null;
  const scoped = (path) => (viewer == null ? path : `${path}${path.includes("?") ? "&" : "?"}person_id=${viewer}`);
  const personName = (personId) => people.find((p) => p.id === personId)?.name;
  function ownerText(account) {
    if (people.length === 0) return null;
    const names = (account.owner_ids || []).map(personName).filter(Boolean);
    if (names.length === 0) return "No owner yet";
    return names.length === 1 ? names[0] : `Shared: ${names.join(" & ")}`;
  }
  const iconSvg = (id, size = 20) => svg("svg", { class: "ico", width: size, height: size, "aria-hidden": "true" }, svg("use", { href: `#${id}` }));

  // A bank's logo (Plaid sends it as a small PNG), or its initials on the bank's colour.
  function readableOn(hex) {
    const n = parseInt(hex.slice(1), 16);
    const lum = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
    return lum > 0.6 ? "#111827" : "#ffffff";
  }
  function bankLogo(item, name, size = 36) {
    if (item && item.logo) {
      const img = el("img", { class: "bank-logo", alt: "", width: size, height: size });
      img.src = `data:image/png;base64,${item.logo}`;
      return img;
    }
    const label = (name || "Bank").trim();
    let hue = 0;
    for (const ch of label) hue = (hue * 31 + ch.charCodeAt(0)) % 360;
    const color = item && /^#[0-9a-fA-F]{6}$/.test(item.color || "") ? item.color : null;
    const badge = el("span", { class: "bank-logo initials", "aria-hidden": "true", text: label.slice(0, 2).toUpperCase() });
    badge.style.width = badge.style.height = `${size}px`;
    badge.style.background = color || `hsl(${hue} 50% 36%)`;
    badge.style.color = color ? readableOn(color) : "#ffffff";
    return badge;
  }

  // The Overview's account sections. Each is its own card with a total, then a table.
  const ACCOUNT_SECTIONS = [
    ["investment", "Investments", "i-trend"], ["depository", "Cash", "i-cash"], ["credit", "Credit", "i-card"],
    ["loan", "Loans", "i-home"], ["other", "Other", "i-list"],
  ];
  function sectionTotals(rows, kind) {
    const totals = {};
    for (const a of rows) if (a.current_balance != null) totals[a.currency] = (totals[a.currency] || 0) + a.current_balance;
    return Object.entries(totals).sort(([a], [b]) => (a === baseCurrency ? -1 : b === baseCurrency ? 1 : a.localeCompare(b)))
      .map(([cur, value]) => money(value, cur)).join(" \u00B7 ") + (kind === "credit" || kind === "loan" ? " owed" : "");
  }
  function ownersCell(a) {
    const names = (a.owner_ids || []).map(personName).filter(Boolean);
    if (names.length === 0) return el("span", { class: "muted", text: "No owner yet" });
    return el("span", {}, names.join(" & "), names.length > 1 && el("span", { class: "pill", text: "Shared" }));
  }
  function accountSection(kind, label, iconId, rows, itemsById) {
    const credit = kind === "credit";
    const showOwner = people.length > 0;
    const open = (a) => { location.hash = `#/transactions?account=${encodeURIComponent(a.id)}`; };
    const headers = [["Account", "col-account"], ["Bank", "col-bank"], showOwner && ["Owner", "col-owner"],
      credit && ["Limit", "num col-extra"], credit && ["Available", "num col-extra"], credit && ["Used", "num col-extra"],
      kind === "depository" && ["Available", "num col-extra"],
      [kind === "investment" ? "Value" : credit || kind === "loan" ? "Owed" : "Balance", "num col-balance"]].filter(Boolean);
    const sorted = rows.slice().sort((a, b) => Math.abs(b.current_balance || 0) - Math.abs(a.current_balance || 0));
    const body = sorted.map((a) => {
      const item = itemsById.get(a.item_id);
      const available = credit && a.credit_limit != null
        ? (a.available_balance ?? (a.current_balance != null ? a.credit_limit - a.current_balance : null)) : null;
      const used = credit && a.credit_limit > 0 && a.current_balance != null ? Math.round((a.current_balance / a.credit_limit) * 100) : null;
      const cells = [
        el("td", { class: "col-account" }, el("div", { class: "acct" }, bankLogo(item, a.institution),
          el("div", {}, el("a", { href: `#/transactions?account=${encodeURIComponent(a.id)}`, class: "acct-name", text: a.name }),
            a.mask && el("span", { class: "muted", text: `  \u2022\u2022${a.mask}` }),
            el("div", { class: "muted narrow-only", text: [a.institution, showOwner ? (ownerText(a) || "") : ""].filter(Boolean).join(" \u00B7 ") })))),
        el("td", { class: "col-bank", text: a.institution || "" }),
        showOwner && el("td", { class: "col-owner" }, ownersCell(a)),
        credit && el("td", { class: "num col-extra", text: a.credit_limit != null ? money(a.credit_limit, a.currency) : "\u2014" }),
        credit && el("td", { class: "num col-extra", text: available != null ? money(available, a.currency) : "\u2014" }),
        credit && el("td", { class: `num col-extra${used != null && used >= 80 ? " neg" : ""}`, text: used != null ? `${used}%` : "\u2014" }),
        kind === "depository" && el("td", { class: "num col-extra", text: a.available_balance != null ? money(a.available_balance, a.currency) : "\u2014" }),
        el("td", { class: "num col-balance" }, a.current_balance == null ? "\u2014" : money(a.current_balance, a.currency)),
      ].filter(Boolean);
      return el("tr", { class: "click", tabindex: 0, onclick: (e) => { if (!e.target.closest("a")) open(a); },
        onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) open(a); } }, cells);
    });
    return el("section", { class: `card acct-section ${kind}` },
      el("div", { class: "section-head" },
        el("div", { class: "section-icon", "aria-hidden": "true" }, iconSvg(iconId, 22)),
        el("div", { class: "grow" }, el("h2", { text: label }), el("div", { class: "muted", text: `${rows.length} account${rows.length === 1 ? "" : "s"}` })),
        el("div", { class: "section-total", text: sectionTotals(rows, kind) })),
      el("div", { class: "table-wrap" }, el("table", { class: "acct-table" },
        el("thead", {}, el("tr", {}, headers.map(([text, cls]) => el("th", { scope: "col", class: cls, text })))),
        el("tbody", {}, body))));
  }

  // A line under the page title in a person's view; empty for the household.
  function viewerNote(householdOnly = false) {
    if (viewer == null) return document.createDocumentFragment();  // nothing to show, and nothing printed
    const name = personName(viewer) || "this person";
    return el("p", { class: "viewer-note", text: householdOnly
      ? `This page covers the whole household, not just ${name}.`
      : `Showing ${name}'s accounts. A shared account counts in full for each owner, so the people's totals add up to more than the household's.` });
  }
  async function loadPeople() {
    try { people = await api("/api/people"); } catch (err) { if (err instanceof SignedOut) throw err; people = []; }
    if (viewer != null && !people.some((p) => p.id === viewer)) viewer = null;
    $("viewer").replaceChildren(el("option", { value: "", text: "Household" }),
      ...people.map((p) => el("option", { value: String(p.id), text: p.name, selected: p.id === viewer })));
    $("viewer-wrap").hidden = people.length === 0;
  }

  async function categoryChoices(extra = []) {
    let fromServer = [];
    try { fromServer = await api("/api/categories"); } catch (err) { if (err instanceof SignedOut) throw err; }
    return [...new Set([...KNOWN_CATEGORIES, ...fromServer, ...extra].filter(Boolean))].sort((a, b) => categoryLabel(a).localeCompare(categoryLabel(b)));
  }

  // A category picker: a list plus "Custom…" with a text box. Returns { node, value() }.
  function categoryPicker(choices, current) {
    const select = el("select", { "aria-label": "Category" },
      choices.map((c) => el("option", { value: c, selected: c === current, text: categoryLabel(c) })),
      el("option", { value: "__custom", text: "Custom…" }));
    const custom = el("input", { placeholder: "New category name", maxlength: 60, hidden: true, "aria-label": "Custom category" });
    select.addEventListener("change", () => { custom.hidden = select.value !== "__custom"; if (!custom.hidden) custom.focus(); });
    return {
      node: el("div", {}, select, custom),
      value: () => (select.value === "__custom" ? custom.value.trim().replace(/\s+/g, "_").toUpperCase() : select.value),
    };
  }

  function openSheet(...content) {
    const sheet = $("sheet");
    sheet.replaceChildren(...content.flat(Infinity).filter((node) => node != null && node !== false));
    if (!sheet.open) sheet.showModal();
    return sheet;
  }
  const closeSheet = () => { if ($("sheet").open) $("sheet").close(); };

  // A centered question box on its own layer, so it can sit on top of another open box. Resolves
  // true only when the confirm button is pressed; Cancel and Esc resolve false. It is closed again
  // before this resolves.
  function confirmBox({ title, intro, points = [], confirmText = "Continue", cancelText = "Cancel", danger = false }) {
    return new Promise((resolve) => {
      let answer = false;
      const box = $("confirm");
      const close = () => { if (box.open) box.close(); };
      box.replaceChildren(el("div", { class: "confirm" },
        el("div", { class: `confirm-mark${danger ? " danger" : ""}`, "aria-hidden": "true", text: "!" }),
        el("h2", { id: "confirm-title", text: title }),
        intro && el("p", { text: intro }),
        points.length > 0 && el("ul", {}, points.map((point) => el("li", { text: point }))),
        el("div", { class: "actions" },
          el("button", { type: "button", class: danger ? "danger-solid" : "primary", text: confirmText, onclick: () => { answer = true; close(); } }),
          el("button", { type: "button", class: "secondary", text: cancelText, onclick: close }))));
      box.addEventListener("close", () => resolve(answer), { once: true });
      box.showModal();
    });
  }

  function guard(promise) {
    return promise.catch((err) => {
      if (err instanceof SignedOut) { showLogin(); return undefined; }
      toast(err.message, true);
      return undefined;
    });
  }

  // ---------------------------------------------------------------- sign-in activity

  const LOGIN_RESULTS = {
    success: ["Signed in", false], bad_password: ["Wrong password", true], bad_code: ["Wrong authenticator code", true],
    unknown_user: ["Unknown username", true], rate_limited: ["Blocked: too many attempts", true],
    locked: ["Refused: sign-in is locked", true], lockout: ["Sign-in locked after too many failures", true],
    unlocked: ["Unlocked from the NAS", false],
  };
  const FAILED = new Set(["bad_password", "bad_code", "unknown_user", "lockout"]);

  function setAlert(log) {
    const node = $("alert");
    const recent = log ? log.events.filter((e) => FAILED.has(e.result) && Date.now() - new Date(e.at).getTime() < 864e5).length : 0;
    if (!log || (!log.locked && recent === 0)) { node.hidden = true; return; }
    node.replaceChildren(
      log.locked ? "New sign-ins are locked after too many failed attempts. " : `${recent} failed sign-in attempt${recent === 1 ? "" : "s"} in the last 24 hours. `,
      el("a", { href: "#/settings?tab=log", text: "See the sign-in activity" }));
    node.hidden = false;
  }

  // ---------------------------------------------------------------- Overview

  let nwDays = 365;

  async function viewOverview(id) {
    const [summary, accounts, log, spending, items] = await Promise.all([
      api(scoped("/api/summary")), api(scoped("/api/accounts?include_hidden=false")), api("/auth/login-log?limit=100").catch(() => null),
      api(scoped("/api/spending")).catch(() => ({ categories: [] })), api("/api/items").catch(() => [])]);  // no month = the current one, same as the summary
    if (id !== navId) return;
    setAlert(log);
    baseCurrency = summary.base_currency;
    setBanner(summary.items_needing_attention);
    const worth = summary.net_worth.find((n) => n.currency === baseCurrency) || summary.net_worth[0];
    const cur = worth ? worth.currency : baseCurrency;

    const chartBox = el("div");
    const drawChart = async () => {
      try {
        const rows = await api(scoped(`/api/networth?days=${nwDays}&currency=${encodeURIComponent(cur)}`));
        if (id === navId) chartBox.replaceChildren(lineChart(rows.map((r) => ({ date: r.date, value: r.net_worth })), cur));
      } catch (err) { if (err instanceof SignedOut) showLogin(); else chartBox.replaceChildren(el("p", { class: "error", text: err.message })); }
    };
    const ranges = [["1M", 30], ["3M", 90], ["1Y", 365], ["All", 3650]];
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Chart range" });
    const paint = () => seg.replaceChildren(...ranges.map(([label, days]) =>
      el("button", { type: "button", "aria-pressed": String(days === nwDays), text: label, onclick: () => { nwDays = days; paint(); drawChart(); } })));
    paint();

    const netCard = el("section", { class: "card" },
      el("h3", { text: "Net worth" }),
      el("div", { class: "big", text: worth ? money(worth.net_worth, cur) : money(0, cur) }),
      worth && el("p", { class: "muted", text: `Assets ${money(worth.assets, cur)} · Liabilities ${money(worth.liabilities, cur)} · Investments ${money(worth.investments, cur)}` }),
      summary.net_worth.filter((n) => n !== worth).map((n) => el("p", { class: "muted", text: `${n.currency}: ${money(n.net_worth, n.currency)}` })),
      el("div", { class: "toolbar" }, seg),
      chartBox,
      el("p", { class: "muted", text: `Last synced ${ago(summary.last_synced_at)}. Pull new bank data from Settings.` }));

    const budgeted = summary.budgeted_this_month;
    const pct = budgeted > 0 ? (summary.spent_this_month / budgeted) * 100 : 0;
    const spendCard = el("section", { class: "card" },
      el("h3", { text: `Spent in ${monthLabel(summary.month)}` }),
      el("div", { class: "big", text: money(summary.spent_this_month, cur) }),
      budgeted > 0 ? [progress(pct), el("p", { class: "muted", text: `of ${money(budgeted, cur)} budgeted (${Math.round(pct)}%)` })]
        : el("p", { class: "muted", text: viewer != null ? "Budgets cover the whole household." : "No budgets set yet." }),
      summary.over_budget.length > 0 && el("div", { class: "chips" }, summary.over_budget.map((b) =>
        el("span", { class: "chip", text: `${categoryLabel(b.category)} ${money(b.spent - b.monthly_limit, cur)} over` }))),
      spending.categories.length > 0 && [el("h3", { text: "Top categories" }),
        spending.categories.slice(0, 5).map((c) => el("div", { class: "row" },
          el("div", { class: "icon", text: icon(c.category), "aria-hidden": "true" }),
          el("div", { class: "grow" }, el("div", { text: categoryLabel(c.category) }),
            progress(spending.total > 0 ? (Math.max(c.spent, 0) / spending.total) * 100 : 0, true)),
          el("div", { class: "amount", text: money(c.spent, cur) }))),
        el("p", {}, el("a", { href: "#/budgets", text: "See budgets and trends" }))]);

    const itemsById = new Map(items.map((i) => [i.id, i]));
    const known = new Set(["investment", "depository", "credit", "loan"]);
    const sections = ACCOUNT_SECTIONS
      .map(([kind, label, iconId]) => [kind, label, iconId, accounts.filter((a) => (kind === "other" ? !known.has(a.type) : a.type === kind))])
      .filter(([, , , rows]) => rows.length > 0)
      .map(([kind, label, iconId, rows]) => accountSection(kind, label, iconId, rows, itemsById));
    if (sections.length === 0) {
      sections.push(el("section", { class: "card" }, el("h2", { text: "Accounts" }),
        el("p", { class: "empty", text: "No accounts yet. Link a bank from Settings." })));
    }
    view().replaceChildren(viewerNote(), el("div", { class: "grid" }, netCard, spendCard), ...sections);
    drawChart();
  }

  // ---------------------------------------------------------------- Transactions

  async function viewTransactions(id, params) {
    const [accounts, choices] = await Promise.all([api(scoped("/api/accounts")), categoryChoices()]);
    if (id !== navId) return;
    const accountName = Object.fromEntries(accounts.map((a) => [a.id, `${a.institution ? a.institution + " " : ""}${a.name}`]));
    const state = { search: "", account: params.get("account") || "", merchant: params.get("merchant") || "", category: "", offset: 0, total: null, loading: false, lastDay: null, rows: new Map() };
    const list = el("div");
    const status = el("p", { class: "empty" });
    const sentinel = el("div");

    const search = el("input", { type: "search", placeholder: "Search merchants", "aria-label": "Search", maxlength: 100 });
    const accountSel = el("select", { "aria-label": "Account" }, el("option", { value: "", text: "All accounts" }),
      accounts.map((a) => el("option", { value: a.id, selected: a.id === state.account, text: accountName[a.id] })));
    const categorySel = el("select", { "aria-label": "Category filter" }, el("option", { value: "", text: "All categories" }),
      choices.map((c) => el("option", { value: c, text: categoryLabel(c) })));

    function addRows(transactions) {
      for (const tx of transactions) {
        state.rows.set(tx.id, tx);
        if (tx.date !== state.lastDay) {
          state.lastDay = tx.date;
          list.append(el("div", { class: "dayhead", text: dayLabel(tx.date) }));
        }
        list.append(txRow(tx));
      }
    }
    function txRow(tx) {
      const amount = signed(tx.amount, tx.currency);
      const row = el("div", { class: "row click", role: "button", tabindex: 0, "data-id": tx.id },
        el("div", { class: "icon", text: icon(tx.category), "aria-hidden": "true" }),
        el("div", { class: "grow" },
          el("div", {}, el("strong", { text: tx.display_name }), tx.pending && el("span", { class: "badge", text: "Pending" })),
          el("div", { class: "muted", text: `${categoryLabel(tx.subcategory || tx.category)} · ${accountName[tx.account_id] || ""}` })),
        el("div", { class: `amount ${amount.cls}`, text: amount.text }));
      const open = () => openTransaction(tx, choices, accountName, (updated) => {
        state.rows.set(updated.id, updated);
        row.replaceWith(txRow(updated));
      });
      row.addEventListener("click", open);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      return row;
    }

    async function loadMore(reset) {
      if (state.loading) return;
      if (reset) { state.offset = 0; state.total = null; state.lastDay = null; state.rows.clear(); list.replaceChildren(); }
      else if (state.total !== null && state.offset >= state.total) return;
      state.loading = true;
      status.textContent = "Loading…";
      try {
        const q = new URLSearchParams({ limit: "50", offset: String(state.offset) });
        if (state.search) q.set("search", state.search);
        if (state.account) q.set("account_id", state.account);
        if (state.merchant) q.set("merchant_id", state.merchant);
        if (state.category) q.set("category", state.category);
        if (viewer != null) q.set("person_id", String(viewer));
        const page = await api(`/api/transactions?${q}`);
        if (id !== navId) return;
        state.total = page.total;
        state.offset += page.transactions.length;
        addRows(page.transactions);
        status.textContent = page.total === 0 ? "No transactions match." : state.offset >= page.total ? "That's everything." : "";
      } catch (err) {
        if (err instanceof SignedOut) showLogin(); else status.textContent = err.message;
      } finally { state.loading = false; }
    }

    let timer;
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.search = search.value.trim(); loadMore(true); }, 300); });
    accountSel.addEventListener("change", () => { state.account = accountSel.value; loadMore(true); });
    categorySel.addEventListener("change", () => { state.category = categorySel.value; loadMore(true); });

    const merchantNote = state.merchant && el("p", { class: "muted" }, "Showing one merchant's transactions. ",
      el("a", { href: "#/transactions", text: "Show all" }));
    view().replaceChildren(viewerNote(), el("section", { class: "card" },
      el("div", { class: "toolbar" }, search, accountSel, categorySel), merchantNote, list, status, sentinel));
    const observer = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting) && id === navId) loadMore(false); });
    observer.observe(sentinel);
    cleanups.push(() => observer.disconnect());
    await loadMore(true);
  }

  function openTransaction(tx, choices, accountName, onChange) {
    const picker = categoryPicker(choices.includes(tx.category) ? choices : [...choices, tx.category].filter(Boolean), tx.category);
    const amount = signed(tx.amount, tx.currency);
    const sourceLabel = { rule: "Set by a rule", manual: "Changed by you", plaid: "From your bank (Plaid)" }[tx.category_source] || "From your bank (Plaid)";
    const message = el("p", { class: "error", role: "alert" });
    const offer = el("div");

    async function change(category) {
      try {
        const updated = await api(`/api/transactions/${encodeURIComponent(tx.id)}`, { method: "PATCH", body: { category } });
        tx = updated;
        onChange(updated);
        toast(category === null ? "Back to the automatic category." : `Moved to ${categoryLabel(updated.category)}.`);
        if (category !== null) await offerRule(updated);
        else closeSheet();
      } catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
    }

    async function offerRule(updated) {
      const field = updated.merchant_name ? "merchant_name" : "name";
      const pattern = updated.merchant_name || updated.name;
      const rule = { match_field: field, match_type: "equals", pattern, category: updated.category };
      let preview;
      try { preview = await api("/api/rules/preview", { method: "POST", body: rule }); }
      catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); return; } closeSheet(); return; }
      const create = async (applyToPast) => {
        try {
          await api("/api/rules", { method: "POST", body: { ...rule, apply_to_past: applyToPast } });
          toast(applyToPast ? "Rule saved and applied to past transactions. Reload to see them." : "Rule saved.");
          closeSheet();
        } catch (err) { message.textContent = err.message; }
      };
      offer.replaceChildren(el("hr"),
        el("h3", { text: `Always categorize “${pattern}” as ${categoryLabel(updated.category)}?` }),
        el("p", { class: "muted", text: `${preview.would_change} existing transaction${preview.would_change === 1 ? "" : "s"} would change` +
          (preview.skipped_manual ? `; ${preview.skipped_manual} you set by hand stay as they are.` : ".") }),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Create rule and apply to past", onclick: () => create(true) }),
          el("button", { type: "button", text: "Create rule (new ones only)", onclick: () => create(false) }),
          el("button", { type: "button", class: "secondary", text: "No thanks", onclick: closeSheet })));
    }

    openSheet(
      el("h2", { text: tx.display_name }),
      el("div", { class: `big ${amount.cls}`, text: amount.text }),
      el("p", { class: "muted", text: `${dayLabel(tx.date)}${tx.pending ? " · Pending" : ""} · ${accountName[tx.account_id] || ""}` }),
      tx.name !== tx.display_name && el("p", { class: "muted", text: `Bank's text: ${tx.name}` }),
      el("p", { class: "muted" }, "Merchant: ",
        tx.merchant ? el("button", { type: "button", class: "link", text: tx.merchant, onclick: () => { closeSheet(); openMerchantById(tx.merchant_id); } }) : "not named yet",
        "  ",
        el("button", { type: "button", class: "link", text: tx.merchant ? "Change name" : "Name it",
          onclick: () => openNameDialog({ pattern: tx.merchant_name || tx.name, field: tx.merchant_name ? "merchant_name" : "name", name: tx.merchant || "" },
            () => { closeSheet(); route(); }) })),
      el("p", { class: "muted", text: sourceLabel + (tx.category_overridden ? ` (bank said: ${categoryLabel(tx.plaid_category)})` : "") }),
      el("label", {}, "Category", picker.node),
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Save category", onclick: () => {
          const value = picker.value();
          if (!value) { message.textContent = "Choose or type a category."; return; }
          change(value);
        } }),
        tx.category_overridden && el("button", { type: "button", text: "Reset to automatic", onclick: () => change(null) }),
        el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })),
      message, offer);
  }

  // ---------------------------------------------------------------- Budgets

  let budgetMonth = null;

  async function viewBudgets(id) {
    if (!budgetMonth) {
      const summary = await api("/api/summary");
      budgetMonth = summary.month;
      baseCurrency = summary.base_currency;
    }
    const month = budgetMonth;
    const [budgets, spending, monthly, choices] = await Promise.all([
      api(`/api/budgets?month=${month}`), api(`/api/spending?month=${month}`), api("/api/spending/monthly?months=12"), categoryChoices()]);
    if (id !== navId) return;
    const cur = budgets.currency;
    const reload = () => guard(viewBudgets(++navId));

    const nav = el("div", { class: "monthnav" },
      el("button", { type: "button", "aria-label": "Previous month", text: "‹", onclick: () => { budgetMonth = addMonths(month, -1); reload(); } }),
      el("strong", { text: monthLabel(month) }),
      el("button", { type: "button", "aria-label": "Next month", text: "›", onclick: () => { budgetMonth = addMonths(month, 1); reload(); } }),
      el("button", { type: "button", class: "primary", text: "Add budget", onclick: () => budgetDialog(null) }));

    function budgetDialog(existing) {
      const used = new Set(budgets.budgets.map((b) => b.category));
      const picker = existing ? null : categoryPicker(choices.filter((c) => !used.has(c)), null);
      const limit = el("input", { type: "number", min: "0.01", step: "0.01", inputmode: "decimal", value: existing ? String(existing.monthly_limit) : "", "aria-label": "Monthly limit" });
      const message = el("p", { class: "error", role: "alert" });
      const save = async () => {
        const category = existing ? existing.category : picker.value();
        const amount = Number(limit.value);
        if (!category) { message.textContent = "Choose a category."; return; }
        if (!(amount > 0)) { message.textContent = "Enter a limit above zero."; return; }
        try {
          await api(`/api/budgets/${encodeURIComponent(category)}`, { method: "PUT", body: { monthly_limit: amount } });
          closeSheet(); toast("Budget saved."); reload();
        } catch (err) { message.textContent = err instanceof SignedOut ? "Signed out." : err.message; }
      };
      openSheet(
        el("h2", { text: existing ? categoryLabel(existing.category) : "Add a budget" }),
        picker && el("label", {}, "Category", picker.node),
        el("label", {}, `Monthly limit (${cur})`, limit),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Save", onclick: save }),
          existing && el("button", { type: "button", class: "danger", text: "Delete budget", onclick: async () => {
            try { await api(`/api/budgets/${encodeURIComponent(existing.category)}`, { method: "DELETE" }); closeSheet(); toast("Budget deleted."); reload(); }
            catch (err) { message.textContent = err.message; }
          } }),
          el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })),
        message);
    }

    const budgetCard = el("section", { class: "card" }, el("h2", { text: "Budgets" }));
    if (budgets.budgets.length === 0) budgetCard.append(el("p", { class: "empty", text: "No budgets yet. Add one to track a category." }));
    const budgeted = new Set(budgets.budgets.map((b) => b.category));
    for (const b of budgets.budgets) {
      const left = b.remaining >= 0 ? `${money(b.remaining, cur)} left` : `${money(-b.remaining, cur)} over`;
      const inside = b.parent && budgeted.has(b.parent) ? ` \u00B7 also counted in ${categoryLabel(b.parent)}` : "";
      budgetCard.append(el("div", { class: `row click${b.parent ? " sub" : ""}`, role: "button", tabindex: 0, onclick: () => budgetDialog(b),
        onkeydown: (e) => { if (e.key === "Enter") budgetDialog(b); } },
        el("div", { class: "icon", text: icon(b.category), "aria-hidden": "true" }),
        el("div", { class: "grow" },
          el("div", {}, el("strong", { text: categoryLabel(b.category) })),
          progress(b.percent),
          el("div", { class: "muted", text: `${money(b.spent, cur)} of ${money(b.monthly_limit, cur)} · ${left}${inside}` }))));
    }

    const maxSpent = Math.max(...spending.categories.map((c) => Math.abs(c.spent)), 1);
    const spendCard = el("section", { class: "card" }, el("h2", { text: "Spending by category" }),
      el("p", { class: "muted", text: `${money(spending.total, cur)} in ${monthLabel(month)}. Transfers, loan and card payments and income are left out.` }));
    if (spending.categories.length === 0) spendCard.append(el("p", { class: "empty", text: "Nothing spent this month." }));
    for (const c of spending.categories) {
      spendCard.append(el("div", { class: "row" },
        el("div", { class: "icon", text: icon(c.category), "aria-hidden": "true" }),
        el("div", { class: "grow" }, el("div", { text: categoryLabel(c.category) }), progress((Math.max(c.spent, 0) / maxSpent) * 100, true)),
        el("div", { class: "amount", text: money(c.spent, cur) })));
      // The sub-categories are a finer view of the same money, so they are indented and never added to the total.
      const split = c.subcategories || [];
      const rest = c.spent - split.reduce((sum, s) => sum + s.spent, 0);
      const parts = split.map((s) => [subLabel(s.category), s.spent]);
      if (split.length > 0 && Math.abs(rest) >= 0.005) parts.push(["Not in a sub-category", rest]);
      for (const [label, spent] of parts) {
        spendCard.append(el("div", { class: "row sub" },
          el("div", { class: "grow" }, el("div", { class: "muted", text: label }), progress((Math.max(spent, 0) / Math.max(c.spent, 0.01)) * 100, true)),
          el("div", { class: "amount muted", text: money(spent, cur) })));
      }
    }
    const trendCard = el("section", { class: "card" }, el("h2", { text: "Last 12 months" }), barChart(monthly, cur));
    view().replaceChildren(viewerNote(true), nav, budgetCard, el("div", { class: "grid" }, spendCard, trendCard));
  }

  // ---------------------------------------------------------------- Investments

  const CHANGE_PERIODS = [["day", "Day"], ["open", "Since open"], ["1w", "1W"], ["1m", "1M"], ["3m", "3M"], ["ytd", "YTD"], ["1y", "1Y"]];
  const PERIOD_HELP = {
    day: "Change since yesterday's close.", open: "Change since today's open.", "1w": "Change over the last 7 days.",
    "1m": "Change over the last month.", "3m": "Change over the last 3 months.", ytd: "Change since the start of the year.", "1y": "Change over the last year.",
  };
  let invPeriod = "day";
  let syncPolls = 0;  // first-sync checks on the Settings screen since you last navigated or linked

  function changeText(change, currency) {
    if (!change) return null;
    const sign = change.amount > 0 ? "+" : change.amount < 0 ? MINUS : "";
    const percent = change.percent == null ? "" : ` (${sign}${Math.abs(change.percent).toFixed(2)}%)`;
    return { text: `${sign}${money(Math.abs(change.amount), currency)}${percent}`, cls: change.amount > 0 ? "income" : change.amount < 0 ? "neg" : "muted" };
  }

  async function viewInvestments(id) {
    let timer = null;
    const paint = (data) => {
      const totals = Object.entries(data.totals);
      const status = data.status;
      const seg = el("div", { class: "seg", role: "group", "aria-label": "Compare against" },
        CHANGE_PERIODS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === invPeriod), text: label,
          onclick: () => { invPeriod = key; paint(data); } })));
      const newest = data.holdings.filter((h) => h.price_time).map((h) => h.price_time).sort().pop();
      const statusLine = !status.enabled
        ? "Live prices are turned off on the server. Showing your bank's last prices."
        : status.message
          ? `Live prices unavailable (${status.message}). ${status.live_positions ? "Showing the last prices received." : "Showing your bank's last prices."}`
          : status.live_positions === 0
            ? "No position has a live price. Showing your bank's last prices."
            : `Live prices for ${status.live_positions} of ${status.positions} positions${newest ? ` (last trade ${new Date(newest).toLocaleTimeString("en-CA")})` : ""}. Updates every minute while this page is open; Yahoo Finance may delay prices by up to 15 minutes.`;
      const head = el("section", { class: "card" }, el("h3", { text: "Total value" }),
        totals.length === 0 ? el("p", { class: "empty", text: "No investment holdings yet. They appear once an investment account is linked and synced." })
          : totals.map(([cur, t]) => {
            const c = changeText(t.changes[invPeriod], cur);
            return el("div", {}, el("div", { class: "big" }, money(t.value, cur), el("span", { class: "muted", text: `  ${cur}` })),
              c ? el("div", { class: c.cls }, el("strong", { text: c.text }), el("span", { class: "muted", text: `  ${PERIOD_HELP[invPeriod]}` }))
                : el("div", { class: "muted", text: "No change available for this period yet." }));
          }),
        el("div", { class: "toolbar" }, seg),
        el("p", { class: status.message || !status.enabled ? "error" : "muted", text: statusLine }),
        el("p", { class: "muted", text: "Changes assume you held today's quantity for the whole period, so a position bought recently shows what it would have done, not what you earned." }));

      const byAccount = new Map();
      for (const h of data.holdings) {
        const key = `${h.account_name}${h.account_mask ? ` \u2022\u2022${h.account_mask}` : ""}`;
        if (!byAccount.has(key)) byAccount.set(key, []);
        byAccount.get(key).push(h);
      }
      const cards = [...byAccount].map(([name, holdings]) => el("section", { class: "card" }, el("h2", { text: name }),
        holdings.map((h) => {
          const pct = h.gain != null && h.cost_basis > 0 ? (h.gain / h.cost_basis) * 100 : null;
          const gainCls = h.gain == null || h.gain === 0 ? "" : h.gain > 0 ? "income" : "neg";
          const c = changeText(h.changes[invPeriod], h.currency);
          const priceNote = h.live ? (h.market_open ? "live" : `last trade ${new Date(h.price_time).toLocaleString("en-CA", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`) : "bank price";
          return el("div", { class: "row" },
            el("div", { class: "grow" },
              el("strong", { text: h.ticker || h.name }), h.live && h.market_open && el("span", { class: "badge", text: "Live" }),
              el("div", { class: "muted", text: `${h.ticker ? h.name + " \u00B7 " : ""}${h.quantity} @ ${h.price != null ? money(h.price, h.currency) : "\u2014"} (${priceNote})` })),
            el("div", { class: "amount" }, h.value != null ? money(h.value, h.currency) : "\u2014",
              c && el("div", { class: c.cls, text: c.text }),
              h.gain != null && el("div", { class: `muted ${gainCls}`, text: `All time ${h.gain > 0 ? "+" : h.gain < 0 ? MINUS : ""}${money(Math.abs(h.gain), h.currency)}${pct != null ? ` (${pct >= 0 ? "+" : MINUS}${Math.abs(pct).toFixed(1)}%)` : ""}` })));
        })));
      view().replaceChildren(viewerNote(), head, ...cards);
    };

    const load = async () => paint(await api(scoped("/api/holdings/live"), { timeout: 40000 }));
    await load();
    if (id !== navId) return;
    timer = setInterval(() => {
      if (document.hidden || id !== navId) return;
      load().catch((err) => { if (err instanceof SignedOut) showLogin(); });
    }, 60000);
    cleanups.push(() => clearInterval(timer));
  }

  // ---------------------------------------------------------------- Merchants

  // ---- Money Management: credit card statements, minimums and due dates ------------------------

  const DUE_PILLS = {
    overdue: ["Overdue", "bad"], due_soon: ["Due soon", "warn"], upcoming: ["Upcoming", ""],
    paid: ["Statement paid", "good"], nothing_owed: ["Nothing owed", "good"], unknown: ["Unconfirmed", ""],
  };

  function dueText(card) {
    const days = card.days_until_due;
    if (card.due_date == null) return "Your bank has not shared a due date.";
    if (card.status === "paid" || card.status === "nothing_owed") return `Next payment date ${shortDate(card.due_date)}.`;
    if (days < 0) return `Was due ${shortDate(card.due_date)}, ${-days} day${days === -1 ? "" : "s"} ago.`;
    if (days === 0) return "Due today.";
    if (days === 1) return `Due tomorrow, ${dayLabel(card.due_date)}.`;
    return `Due ${dayLabel(card.due_date)}, in ${days} days.`;
  }

  async function viewMoney(id) {
    const data = await api(scoped("/api/payments-due"));
    if (id !== navId) return;
    const stat = (label, value) => el("div", {}, el("div", { class: "muted", text: label }), el("strong", { text: value }));
    const unpaid = data.cards.filter((c) => c.status !== "paid" && c.status !== "nothing_owed" && c.due_date);
    const next = unpaid.slice().sort((a, b) => a.due_date.localeCompare(b.due_date))[0];

    const summary = el("section", { class: "card" }, el("h2", { text: "Credit card payments" }),
      data.cards.length === 0
        ? el("p", { class: "empty", text: "No credit cards yet. They appear once a bank with a credit card is linked and synced." })
        : [
          Object.entries(data.totals).map(([cur, t]) => el("div", { class: "stats" },
            stat(`Statement balances to pay (${cur})`, money(t.statement_due, cur)),
            stat("Minimum payments", money(t.minimum_due, cur)),
            stat("Total owed on cards", money(t.owed, cur)))),
          next && el("p", { text: `Next payment: ${next.institution} ${next.name}${next.mask ? ` \u2022\u2022${next.mask}` : ""}. ${dueText(next)}` }),
          !data.dues_available && el("p", { class: "muted", text: "Your banks have not shared statement balances or due dates yet. Not every bank does. Banks linked before this page existed may need to be removed and linked again from Settings so Plaid can ask for it. Balances below are still current." }),
          el("p", { class: "muted", text: "Information only. HomeFinance cannot move money, so pay your cards at your bank." }),
        ]);

    const rows = data.cards.map((c) => {
      const [label, tone] = DUE_PILLS[c.status];
      const cur = c.currency;
      return el("section", { class: `card due ${c.status}` },
        el("div", { class: "row" },
          el("div", { class: "grow" },
            el("div", {}, el("strong", { text: `${c.institution} ${c.name}` }), c.mask && el("span", { class: "muted", text: `  \u2022\u2022${c.mask}` }),
              el("span", { class: `pill ${tone}`, text: label })),
            el("div", { class: c.status === "overdue" ? "error" : "muted", text: dueText(c) })),
          el("div", { class: "amount" },
            el("div", { class: "big small", text: money(c.statement_balance ?? c.current_balance, cur) }),
            el("div", { class: "muted", text: c.statement_balance == null ? "balance" : "statement balance" }))),
        el("div", { class: "stats" },
          stat("Minimum payment", c.minimum_payment == null ? "\u2014" : money(c.minimum_payment, cur)),
          stat("Balance now", money(c.current_balance, cur)),
          stat("Available credit", c.available_credit == null ? "\u2014" : money(c.available_credit, cur)),
          stat("Last payment", c.last_payment_amount == null ? "\u2014" : `${money(c.last_payment_amount, cur)}${c.last_payment_date ? ` on ${shortDate(c.last_payment_date)}` : ""}`)),
        c.dues_known && el("p", { class: "muted", text: `${c.statement_date ? `Statement closed ${shortDate(c.statement_date)}.${c.statement_paid && c.status !== "nothing_owed" ? " It has been paid in full." : ""} ` : ""}Bank data updated ${ago(c.dues_updated_at)}.` }));
    });
    view().replaceChildren(viewerNote(), summary, ...rows);
  }

  const MERCH_PERIODS = [["month", "Month"], ["3m", "3M"], ["12m", "12M"], ["all", "All"]];
  const FIELD_LABELS = { name: "the description", merchant_name: "the bank's merchant name" };
  let merchPeriod = "12m";
  let merchAll = false;
  let merchTab = "merchants";

  function shortDate(text) {
    return parseDay(text).toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" });
  }

  async function viewMerchants(id) {
    const [data, todo] = await Promise.all([api(`/api/merchants?period=${merchPeriod}`), api("/api/merchants/unassigned")]);
    if (id !== navId) return;
    baseCurrency = data.currency;
    const cur = data.currency;
    const reload = () => guard(viewMerchants(++navId));

    const seg = el("div", { class: "seg", role: "group", "aria-label": "Period" },
      MERCH_PERIODS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === merchPeriod), text: label,
        onclick: () => { merchPeriod = key; reload(); } })));
    const search = el("input", { type: "search", placeholder: "Search merchants", "aria-label": "Search merchants", maxlength: 100 });
    const includeAll = el("input", { type: "checkbox", checked: merchAll, onchange: (e) => { merchAll = e.target.checked; draw(); } });
    const list = el("div");
    const total = el("p", { class: "muted" });

    function draw() {
      const needle = search.value.trim().toLowerCase();
      const rows = data.merchants.filter((m) =>
        (!needle || m.name.toLowerCase().includes(needle)) && (merchAll ? true : m.spent !== 0));
      const spent = rows.reduce((sum, m) => sum + m.spent, 0);
      total.textContent = `${rows.length} merchant${rows.length === 1 ? "" : "s"}${merchAll ? "" : " with spending"}; ${money(spent, cur)} spent in this period.`;
      list.replaceChildren(...(rows.length === 0
        ? [el("p", { class: "empty", text: data.merchants.length === 0 ? "No merchants yet. They appear after your first sync." : "No merchants match." })]
        : rows.map((m) => {
          const right = m.spent !== 0 ? money(m.spent, cur) : m.outflow > 0 ? `paid ${money(m.outflow, cur)}` : m.inflow > 0 ? `+${money(m.inflow, cur)} in` : "\u2014";
          const open = () => openMerchantById(m.id, reload);
          const row = el("div", { class: "row click", role: "button", tabindex: 0, onclick: open,
            onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } } },
            el("div", { class: "icon", text: icon(m.top_category), "aria-hidden": "true" }),
            el("div", { class: "grow" },
              el("div", {}, el("strong", { text: m.name }), !m.auto && el("span", { class: "badge", text: "named by you" })),
              el("div", { class: "muted", text: `${m.transactions} transaction${m.transactions === 1 ? "" : "s"}${m.last_date ? ` \u00B7 last ${shortDate(m.last_date)}` : ""}${m.top_category ? ` \u00B7 ${categoryLabel(m.top_category)}` : ""}` })),
            el("div", { class: "amount", text: right }));
          return row;
        })));
    }
    search.addEventListener("input", draw);

    const todoCard = el("section", { class: "card" },
      el("h2", { text: "Needs a name" }),
      el("p", { class: "muted", text: "Your bank gave these no clear merchant name. Name one and every matching transaction, past and future, is tracked under it." }),
      todo.length === 0
        ? el("p", { class: "empty", text: "Everything has a name." })
        : todo.map((t) => el("div", { class: "row" },
          el("div", { class: "grow" }, el("strong", { text: t.name }),
            el("div", { class: "muted", text: `${t.transactions} transaction${t.transactions === 1 ? "" : "s"} \u00B7 last ${shortDate(t.last_date)} \u00B7 ${t.account_name}` })),
          el("div", { class: "amount", text: t.outflow ? money(t.outflow, t.currency) : "" }),
          el("button", { type: "button", class: "primary small", text: "Name it", onclick: () => openNameDialog({ pattern: t.name, field: "name" }, reload) }))));

    const masterCard = el("section", { class: "card" }, el("h2", { text: "Merchants" }),
      el("div", { class: "toolbar" }, seg, search),
      el("label", { class: "checkline" }, includeAll, "Include payments, transfers and income"),
      total, list,
      el("div", { class: "actions" }, el("button", { type: "button", text: "Add a merchant", onclick: () => openAddMerchant(reload) })));

    const tabs = el("div", { class: "seg", role: "group", "aria-label": "Merchants page" },
      [["merchants", "Merchants"], ["needs", `Needs a name (${todo.length})`]].map(([key, label]) =>
        el("button", { type: "button", "aria-pressed": String(key === merchTab), text: label,
          onclick: () => { merchTab = key; show(); } })));
    function show() {
      for (const b of tabs.children) b.setAttribute("aria-pressed", String(b.textContent.startsWith(merchTab === "needs" ? "Needs" : "Merchants")));
      view().replaceChildren(viewerNote(true), el("div", { class: "toolbar" }, tabs), merchTab === "needs" ? todoCard : masterCard);
      if (merchTab === "merchants") draw();
    }
    show();
  }

  function openAddMerchant(done) {
    const name = el("input", { maxlength: 80, placeholder: "e.g. Corner Store", "aria-label": "Merchant name" });
    const message = el("p", { class: "error", role: "alert" });
    openSheet(el("h2", { text: "Add a merchant" }),
      el("p", { class: "muted", text: "Creates an empty merchant. Attach bank text to it afterwards with Name it." }),
      el("label", {}, "Name", name), message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Add", onclick: async () => {
          try { await api("/api/merchants", { method: "POST", body: { name: name.value } }); closeSheet(); toast("Merchant added."); done(); }
          catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
        } }),
        el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
  }

  // Give some bank text a name. `done` runs after it is saved.
  async function openNameDialog({ pattern = "", field = "name", kind = "equals", name = "" }, done) {
    let existing = [];
    try { existing = (await api("/api/merchants?period=all")).merchants.map((m) => m.name); } catch (err) { if (err instanceof SignedOut) { showLogin(); return; } }
    const patternInput = el("input", { value: pattern, maxlength: 200, "aria-label": "Bank text" });
    const kindSel = el("select", { "aria-label": "How to match" },
      [["equals", "is exactly"], ["contains", "contains"]].map(([v, l]) => el("option", { value: v, selected: v === kind, text: l })));
    const fieldSel = el("select", { "aria-label": "Which text" },
      Object.entries(FIELD_LABELS).map(([v, l]) => el("option", { value: v, selected: v === field, text: l })));
    const nameInput = el("input", { value: name, maxlength: 80, list: "merchant-names", placeholder: "e.g. Corner Store", "aria-label": "Merchant name", autocomplete: "off" });
    const datalist = el("datalist", { id: "merchant-names" }, existing.map((n) => el("option", { value: n })));
    const previewBox = el("div", { class: "preview", hidden: true });
    const message = el("p", { class: "error", role: "alert" });

    let timer = null;
    const refreshPreview = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const text = patternInput.value.trim();
        if (!text) { previewBox.hidden = true; return; }
        try {
          const p = await api("/api/merchants/aliases/preview", { method: "POST", body: { match_field: fieldSel.value, match_type: kindSel.value, pattern: text } });
          const plural = (n) => `${n} transaction${n === 1 ? "" : "s"}`;
          previewBox.replaceChildren(
            p.matching_transactions === 0 ? "Nothing matches yet. It will apply to future transactions."
              : p.would_apply === 0 ? `${plural(p.matching_transactions)} match, but a name you already gave them is more specific, so none would change.`
                : `${plural(p.would_apply)} would be named${p.currently_unassigned ? ` (${p.currently_unassigned} not yet named)` : ""}${p.kept_by_other_names ? `; ${p.kept_by_other_names} more keep a more specific name` : ""}.`,
            ...p.samples.map((s) => el("div", { class: "muted", text: `${shortDate(s.date)} \u00B7 ${money(Math.abs(s.amount), s.currency)} \u00B7 ${s.account_name} \u00B7 ${s.name}` })));
          previewBox.hidden = false;
        } catch (err) { if (!(err instanceof SignedOut)) { previewBox.textContent = err.message; previewBox.hidden = false; } }
      }, 250);
    };
    for (const input of [patternInput, kindSel, fieldSel]) input.addEventListener("input", refreshPreview);

    openSheet(el("h2", { text: "Name this merchant" }),
      el("p", { class: "muted", text: "Pick the bank text to recognise, and the name to show and track it under. Matching is not case-sensitive." }),
      el("div", { class: "toolbar" }, el("span", { class: "muted", text: "When" }), fieldSel, kindSel),
      el("label", {}, "Bank text", patternInput),
      previewBox,
      el("label", {}, "Merchant name (pick an existing one to combine them)", nameInput), datalist, message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Save name", onclick: async () => {
          message.textContent = "";
          try {
            const r = await api("/api/merchants/aliases", { method: "POST", body: { match_field: fieldSel.value, match_type: kindSel.value, pattern: patternInput.value, name: nameInput.value } });
            closeSheet();
            toast(`${r.updated_transactions} transaction${r.updated_transactions === 1 ? "" : "s"} now tracked as ${r.merchant.name}.`);
            done();
          } catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
        } }),
        el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
    refreshPreview();
  }

  async function openMerchantById(merchantId, done = () => route()) {
    openSheet(el("p", { class: "empty", text: "Loading\u2026" }));
    let d;
    try { d = await api(`/api/merchants/${merchantId}?months=12`); }
    catch (err) { closeSheet(); if (err instanceof SignedOut) showLogin(); else toast(err.message, true); return; }
    const cur = baseCurrency;
    const again = () => openMerchantById(merchantId, done);
    const stat = (label, value) => el("div", {}, el("div", { class: "muted", text: label }), el("strong", { text: value }));
    const message = el("p", { class: "error", role: "alert" });
    const run = async (fn, after) => {
      message.textContent = "";
      try { await fn(); after(); } catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
    };

    openSheet(
      el("h2", {}, d.name, !d.auto && el("span", { class: "badge", text: "named by you" })),
      el("div", { class: "stats" },
        stat("Spent, last 12 months", money(d.spent, cur)),
        stat("Transactions", String(d.transactions)),
        stat("Average charge", d.average_transaction == null ? "\u2014" : money(d.average_transaction, cur)),
        stat("All time", money(d.lifetime.spent, cur)),
        stat("First seen", d.first_date ? shortDate(d.first_date) : "\u2014"),
        stat("Last seen", d.last_date ? shortDate(d.last_date) : "\u2014")),
      d.spent === 0 && d.outflow > 0 && el("p", { class: "muted", text: `This is not counted as spending (payments, transfers and income are left out), but ${money(d.outflow, cur)} left your accounts.` }),
      el("h3", { text: "Spending by month" }), barChart(d.monthly, cur),
      d.categories.length > 0 && [el("h3", { text: "Categories" }), d.categories.slice(0, 4).map((c) =>
        el("div", { class: "row" }, el("div", { class: "grow", text: categoryLabel(c.category) }), el("div", { class: "amount", text: money(c.amount, cur) })))],
      el("h3", { text: "Bank text that maps here" }),
      d.alias_list.map((a) => el("div", { class: "row" },
        el("div", { class: "grow" }, el("div", { text: `\u201C${a.pattern}\u201D` }),
          el("div", { class: "muted", text: `${FIELD_LABELS[a.match_field]} ${a.match_type === "equals" ? "is exactly" : "contains"} \u00B7 ${a.source === "user" ? "added by you" : "from your bank"}` })),
        el("button", { type: "button", class: "danger small", text: "Remove", onclick: () => run(
          () => api(`/api/merchants/aliases/${a.id}`, { method: "DELETE" }), () => { toast("Removed."); done(); again(); }) }))),
      el("p", {}, el("button", { type: "button", class: "link", text: "Add more bank text to this merchant",
        onclick: () => openNameDialog({ pattern: "", field: "name", name: d.name }, () => { done(); again(); }) })),
      d.recent_transactions.length > 0 && [el("h3", { text: "Recent" }), d.recent_transactions.slice(0, 5).map((t) => {
        const a = signed(t.amount, t.currency);
        return el("div", { class: "row" }, el("div", { class: "grow" }, el("div", { text: t.name }), el("div", { class: "muted", text: shortDate(t.date) })),
          el("div", { class: `amount ${a.cls}`, text: a.text }));
      }), el("p", {}, el("a", { href: `#/transactions?merchant=${d.id}`, onclick: closeSheet, text: `See all transactions for ${d.name}` }))],
      message,
      el("div", { class: "actions" },
        el("button", { type: "button", text: "Rename", onclick: () => openRename(d, () => { done(); again(); }) }),
        el("button", { type: "button", text: "Merge into\u2026", onclick: () => openMerge(d, done) }),
        // Deleting a merchant your bank named, with nothing of yours attached, would change nothing: it comes straight back.
        (!d.auto || d.alias_list.some((a) => a.source === "user")) && el("button", { type: "button", class: "danger", text: d.auto ? "Delete" : "Delete or reset", onclick: async () => {
          if (!(await confirmBox({
            title: `Delete ${d.name}?`, intro: "This forgets the merchant and every name you attached to it.",
            points: ["Transactions your bank named go back to an automatic merchant.", "The rest go back to \u201CNeeds a name\u201D."],
            confirmText: "Delete merchant", cancelText: "Keep it", danger: true }))) return;
          run(() => api(`/api/merchants/${d.id}`, { method: "DELETE" }), () => { closeSheet(); toast("Merchant deleted."); done(); });
        } }),
        el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })));
  }

  function openRename(d, done) {
    const name = el("input", { value: d.name, maxlength: 80, "aria-label": "Merchant name" });
    const message = el("p", { class: "error", role: "alert" });
    openSheet(el("h2", { text: "Rename merchant" }),
      el("p", { class: "muted", text: "The new name shows everywhere. Matching transactions stay attached, including future ones." }),
      el("label", {}, "Name", name), message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
          try { await api(`/api/merchants/${d.id}`, { method: "PATCH", body: { name: name.value } }); toast("Renamed."); done(); }
          catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
        } }),
        el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
  }

  async function openMerge(d, done) {
    let others;
    try { others = (await api("/api/merchants?period=all")).merchants.filter((m) => m.id !== d.id); }
    catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else toast(err.message, true); return; }
    others.sort((a, b) => a.name.localeCompare(b.name));
    const pick = el("select", { "aria-label": "Merge into" }, others.map((m) => el("option", { value: m.id, text: m.name })));
    const message = el("p", { class: "error", role: "alert" });
    openSheet(el("h2", { text: `Merge ${d.name}` }),
      el("p", { class: "muted", text: "Everything tracked under this merchant moves to the one you pick, and this one disappears. Future transactions follow too." }),
      others.length === 0 ? el("p", { class: "empty", text: "There is no other merchant to merge into." }) : el("label", {}, "Merge into", pick), message,
      el("div", { class: "actions" },
        others.length > 0 && el("button", { type: "button", class: "primary", text: "Merge", onclick: async () => {
          try { await api(`/api/merchants/${d.id}/merge`, { method: "POST", body: { into_id: Number(pick.value) } }); closeSheet(); toast("Merged."); done(); }
          catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
        } }),
        el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
  }

  // ---------------------------------------------------------------- Settings

  // Plaid Link is Plaid's own script. It is fetched only when you press a link button.
  const PLAID_LINK_SCRIPT = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
  let plaidLoading = null;
  function loadPlaidLink() {
    if (window.Plaid) return Promise.resolve();
    plaidLoading ??= new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = PLAID_LINK_SCRIPT;
      script.onload = resolve;
      script.onerror = () => { plaidLoading = null; script.remove(); reject(new Error("Could not load Plaid Link. Is this device online?")); };
      document.head.append(script);
    });
    return plaidLoading;
  }

  // Link a new bank (item = null) or reconnect one that needs attention (item = the bank).
  async function startLink(item, done) {
    let environment = null;
    try { environment = (await (await plainFetch("/health")).json()).plaid_env; } catch (_) { /* the link call will report a real problem */ }
    if (!item && environment === "production" && !(await confirmBox({
      title: "Link a real bank?",
      intro: "This connects a real bank account through Plaid. It is read-only: HomeFinance can look at your accounts but can never move money.",
      points: [
        "It uses one of the 10 bank slots on your free Plaid plan.",
        "Removing a bank later does not give its slot back, so link each bank only once.",
        "If a bank later needs attention, use Reconnect instead of removing it.",
        "Have your bank login and your phone ready. The bank may text you a code.",
      ],
      confirmText: "Link this bank",
      cancelText: "Not now",
    }))) return;
    let linkToken;
    try {
      await loadPlaidLink();
      linkToken = (await api("/api/link/token", { method: "POST", body: item ? { item_id: item.id } : {} })).link_token;
    } catch (err) {
      if (err instanceof SignedOut) showLogin(); else toast(err.message, true);
      return;
    }
    window.Plaid.create({
      token: linkToken,
      onSuccess: async (publicToken) => {
        try {
          if (item) {
            await api("/api/link/complete", { method: "POST", body: { item_id: item.id } });
            toast(`${item.institution || "Bank"} reconnected. It is syncing now.`);
          } else {
            const added = await api("/api/link/exchange", { method: "POST", body: { public_token: publicToken } });
            toast(`${added.institution || "Bank"} connected. The first sync runs in the background.`);
          }
          syncPolls = 0;
          done();
        } catch (err) {
          if (err instanceof SignedOut) showLogin(); else toast(err.message, true);
        }
      },
      onExit: (err) => { if (err) toast(err.display_message || err.error_message || "Linking was cancelled.", true); },
    }).open();
  }

  // Colour scheme, accent and density. Kept in this browser only (see theme.js).
  function appearanceCard() {
    const card = el("section", { class: "card" });
    const paint = () => {
      const t = window.HFTheme.get();
      const choose = (part, value) => { window.HFTheme.set({ [part]: value }); paint(); };
      const segment = (part, options) => el("div", { class: "seg", role: "group", "aria-label": part },
        options.map(([value, label]) => el("button", { type: "button", "aria-pressed": String(t[part] === value), text: label, onclick: () => choose(part, value) })));
      card.replaceChildren(
        el("h2", { text: "Appearance" }),
        el("p", { class: "muted", text: "Saved in this browser only, so each device can look different." }),
        el("div", { class: "setting" }, el("div", {}, el("strong", { text: "Colour scheme" }), el("div", { class: "muted", text: "System follows your device's light or dark setting." })),
          segment("mode", [["system", "System"], ["light", "Light"], ["dark", "Dark"]])),
        el("div", { class: "setting" }, el("div", {}, el("strong", { text: "Accent colour" }), el("div", { class: "muted", text: `Currently ${t.accent}.` })),
          el("div", { class: "swatches", role: "group", "aria-label": "Accent colour" },
            window.HFTheme.ACCENTS.map((accent) => el("button", { type: "button", class: "swatch", "data-accent": accent, title: accent,
              "aria-label": accent, "aria-pressed": String(t.accent === accent), onclick: () => choose("accent", accent) })))),
        el("div", { class: "setting" }, el("div", {}, el("strong", { text: "Density" }), el("div", { class: "muted", text: "Compact fits more on screen." })),
          segment("density", [["comfortable", "Comfortable"], ["compact", "Compact"]])));
    };
    paint();
    return card;
  }

  const SETTINGS_TABS = [["banks", "Banks"], ["accounts", "Accounts"], ["people", "People"], ["rules", "Rules"], ["appearance", "Appearance"], ["devices", "Devices"], ["log", "Sign-in log"]];
  let settingsTab = "banks";

  async function viewSettings(id, params = new URLSearchParams()) {
    if (SETTINGS_TABS.some(([key]) => key === params.get("tab"))) settingsTab = params.get("tab");
    const [items, sessions, rules, accounts, choices, log] = await Promise.all([
      api("/api/items"), api("/auth/sessions"), api("/api/rules"), api("/api/accounts"), categoryChoices(), api("/auth/login-log?limit=50")]);
    if (id !== navId) return;
    setAlert(log);
    const reload = () => guard(viewSettings(++navId));
    await loadPeople();  // the picker and the owner choices below

    const syncOut = el("div");
    const syncButton = el("button", { type: "button", class: "primary", text: "Sync now", onclick: async () => {
      syncButton.disabled = true; syncButton.textContent = "Syncing… this can take a few minutes";
      try {
        const results = await api("/api/sync", { method: "POST", timeout: 180000 });
        syncOut.replaceChildren(...results.map((r) => el("p", { class: r.ok ? "muted" : "error",
          text: `${r.institution || "Bank"}: ${r.ok ? `${r.transactions_changed} transaction changes` : r.error}` })));
        toast("Sync finished.");
      } catch (err) {
        if (err instanceof SignedOut) showLogin();
        else if (err.status === 409) toast("A sync is already running.");
        else toast(err.message, true);
      } finally { syncButton.disabled = false; syncButton.textContent = "Sync now"; }
    } });

    const linkButton = el("button", { type: "button", class: "primary", text: "Link a bank", onclick: () => startLink(null, reload) });
    const banks = el("section", { class: "card" }, el("h2", { text: "Banks" }),
      el("div", { class: "actions" }, linkButton, syncButton), syncOut,
      items.length === 0 && el("p", { class: "empty", text: "No banks linked yet. Press Link a bank to add your first." }),
      items.map((i) => el("div", { class: "row" }, bankLogo(i, i.institution, 36), el("div", { class: "grow" }, el("strong", { text: i.institution || "Bank" }),
        el("div", { class: i.needs_reauth ? "error" : "muted", text: i.needs_reauth ? "Needs reconnecting" : i.last_synced_at ? `Last synced ${ago(i.last_synced_at)}` : "Waiting for the first sync\u2026" })),
        i.needs_reauth && el("button", { type: "button", class: "primary", text: "Reconnect", onclick: () => startLink(i, reload) }),
        el("button", { type: "button", class: "danger", text: "Remove", onclick: async () => {
          const name = i.institution || "this bank";
          if (!(await confirmBox({
            title: `Remove ${name}?`,
            intro: "This deletes the bank's accounts, transactions and holdings from your server, and closes the connection at Plaid.",
            points: ["The Plaid bank slot it used is not given back.", "If the bank only needs attention, use Reconnect instead."],
            confirmText: "Remove bank", cancelText: "Keep it", danger: true }))) return;
          try { await api(`/api/items/${encodeURIComponent(i.id)}`, { method: "DELETE" }); toast(`${name} removed.`); reload(); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
        } }))));
    // A bank still waiting for its first sync: check again every 6 seconds, at most 10 times,
    // and never while a dialog is open.
    if (items.some((i) => !i.last_synced_at && !i.needs_reauth) && syncPolls < 10) {
      const timer = setTimeout(() => { if ($("sheet").open || $("confirm").open || settingsTab !== "banks") return; syncPolls += 1; reload(); }, 6000);
      cleanups.push(() => clearTimeout(timer));
    }

    const devices = el("section", { class: "card" }, el("h2", { text: "Signed-in devices" }),
      sessions.map((s) => el("div", { class: "row" }, el("div", { class: "grow" },
        el("strong", { text: s.device_name }), s.current && el("span", { class: "badge", text: "This device" }),
        el("div", { class: "muted", text: `Last used ${ago(s.last_used_at)}` })),
        el("button", { type: "button", class: "danger", text: "Revoke", onclick: async () => {
          if (!(await confirmBox({
            title: `Sign out ${s.device_name}?`,
            intro: s.current ? "You will be signed out here and asked for your password and authenticator code again." : "That device will need your password and authenticator code to get back in.",
            confirmText: "Sign out", cancelText: "Cancel", danger: true }))) return;
          try { await api(`/auth/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" }); if (s.current) showLogin(); else reload(); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
        } }))),
      el("div", { class: "actions" }, el("button", { type: "button", text: "Sign out", onclick: signOut })));

    const rulesCard = el("section", { class: "card" }, el("h2", { text: "Categorization rules" }),
      el("p", { class: "muted", text: "A rule recategorizes matching transactions. What you set by hand always wins, then rules, then your bank's category." }),
      rules.length === 0 && el("p", { class: "empty", text: "No rules yet. Change a transaction's category to be offered one." }),
      rules.map((r) => el("div", { class: "row" }, el("div", { class: "grow" },
        el("div", {}, el("strong", { text: r.pattern }), el("span", { class: "muted", text: `  (${r.match_field === "merchant_name" ? "merchant" : "name"} ${r.match_type})` })),
        el("div", { class: "muted", text: `→ ${categoryLabel(r.category)}` })),
        el("button", { type: "button", text: "Edit", onclick: () => ruleDialog(r) }))));

    function ruleDialog(rule) {
      const picker = categoryPicker(choices.includes(rule.category) ? choices : [...choices, rule.category], rule.category);
      const message = el("p", { class: "error", role: "alert" });
      openSheet(el("h2", { text: `Rule: ${rule.pattern}` }), el("label", {}, "Category", picker.node),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
            try { await api(`/api/rules/${encodeURIComponent(rule.id)}`, { method: "PATCH", body: { category: picker.value() } }); closeSheet(); reload(); }
            catch (err) { message.textContent = err.message; }
          } }),
          el("button", { type: "button", text: "Apply to existing", onclick: async () => {
            try { const r = await api(`/api/rules/${encodeURIComponent(rule.id)}/apply`, { method: "POST" }); toast(`${r.updated_transactions} transactions updated.`); }
            catch (err) { message.textContent = err.message; }
          } }),
          el("button", { type: "button", class: "danger", text: "Delete rule", onclick: async () => {
            try { await api(`/api/rules/${encodeURIComponent(rule.id)}`, { method: "DELETE" }); closeSheet(); reload(); }
            catch (err) { message.textContent = err.message; }
          } }),
          el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })), message);
    }

    // Who owns which account. A shared account has several owners and still counts once in the household.
    async function putOwners(path, personIds, done) {
      try { await api(path, { method: "PUT", body: { person_ids: personIds } }); done(); }
      catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
    }
    const unowned = people.length > 0 ? accounts.filter((a) => !a.hidden && (a.owner_ids || []).length === 0).length : 0;
    const banksOf = new Map();
    for (const a of accounts) banksOf.set(a.item_id, [...(banksOf.get(a.item_id) || []), a]);
    const accountsCard = el("section", { class: "card" }, el("h2", { text: "Accounts" }),
      el("p", { class: "muted", text: people.length > 0
        ? "Choose who owns each account. Give an account more than one owner to share it; it still counts once in the household totals. Tick Hide to leave an account out of totals and budgets."
        : "Add the people in your household on the People tab, then choose who owns each account here. Tick Hide to leave an account out of totals and budgets." }),
      unowned > 0 && el("p", { class: "muted", text: `${unowned} account${unowned === 1 ? " has" : "s have"} no owner yet. They count in the household but not in anyone's own view.` }),
      [...banksOf.entries()].map(([itemId, group]) => [
        el("div", { class: "bank-head" }, el("h3", { text: group[0].institution || "Bank" }),
          people.length > 0 && el("div", { class: "actions" },
            people.map((p) => el("button", { type: "button", class: "small", text: `All ${p.name}`,
              onclick: () => putOwners(`/api/items/${encodeURIComponent(itemId)}/owners`, [p.id], () => { toast(`Every ${group[0].institution || "bank"} account is now ${p.name}'s.`); reload(); }) })),
            people.length > 1 && el("button", { type: "button", class: "small", text: "Shared by everyone",
              onclick: () => putOwners(`/api/items/${encodeURIComponent(itemId)}/owners`, people.map((p) => p.id), () => { toast("Shared by everyone."); reload(); }) }))),
        group.map((a) => {
          const chosen = new Set(a.owner_ids || []);
          return el("div", { class: "row" }, el("div", { class: "grow" },
            el("div", {}, el("strong", { text: a.name }), a.mask && el("span", { class: "muted", text: `  \u2022\u2022${a.mask}` })),
            people.length > 0 && el("div", { class: "owners", role: "group", "aria-label": `Owners of ${a.name}` }, people.map((p) =>
              el("label", {}, el("input", { type: "checkbox", checked: chosen.has(p.id), onchange: (e) => {
                if (e.target.checked) chosen.add(p.id); else chosen.delete(p.id);
                putOwners(`/api/accounts/${encodeURIComponent(a.id)}/owners`, [...chosen], () => toast("Saved."));
              } }), p.name)))),
            el("label", { class: "checkline" }, el("input", { type: "checkbox", checked: a.hidden, onchange: async (e) => {
              try { await api(`/api/accounts/${encodeURIComponent(a.id)}`, { method: "PATCH", body: { hidden: e.target.checked } }); toast(e.target.checked ? "Hidden." : "Shown."); }
              catch (err) { e.target.checked = !e.target.checked; if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
            } }), "Hide"));
        })]));

    // The household's people. They are labels for organising accounts, not logins.
    const newName = el("input", { maxlength: 40, placeholder: "Name, for example Sam", "aria-label": "New person's name" });
    const addPerson = async () => {
      try { await api("/api/people", { method: "POST", body: { name: newName.value } }); toast("Added."); reload(); }
      catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
    };
    newName.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addPerson(); } });
    function renamePerson(p) {
      const name = el("input", { value: p.name, maxlength: 40, "aria-label": "Name" });
      const message = el("p", { class: "error", role: "alert" });
      openSheet(el("h2", { text: `Rename ${p.name}` }), el("label", {}, "Name", name),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
            try { await api(`/api/people/${p.id}`, { method: "PATCH", body: { name: name.value } }); closeSheet(); toast("Renamed."); reload(); }
            catch (err) { message.textContent = err.message; }
          } }),
          el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })), message);
    }
    const peopleCard = el("section", { class: "card" }, el("h2", { text: "People" }),
      el("p", { class: "muted", text: "The people whose accounts are linked here. They are labels for organising accounts, not logins: anyone who signs in can see everything. Once you add someone, a menu at the top lets you look at the whole household or one person." }),
      people.length === 0 && el("p", { class: "empty", text: "Nobody yet. Add yourself and anyone else whose accounts are linked here." }),
      people.map((p) => el("div", { class: "row" }, el("div", { class: "grow" }, el("strong", { text: p.name }),
        el("div", { class: "muted", text: `${p.accounts} account${p.accounts === 1 ? "" : "s"}` })),
        el("button", { type: "button", text: "Rename", onclick: () => renamePerson(p) }),
        el("button", { type: "button", class: "danger", text: "Remove", onclick: async () => {
          if (!(await confirmBox({
            title: `Remove ${p.name}?`,
            intro: `${p.name}'s accounts are kept, but they will no longer be owned by ${p.name}.`,
            points: [`${p.name}'s own saved net-worth history is deleted. The household's history is not touched.`],
            confirmText: "Remove person", cancelText: "Keep them", danger: true }))) return;
          try { await api(`/api/people/${p.id}`, { method: "DELETE" }); toast(`${p.name} removed.`); reload(); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
        } }))),
      el("div", { class: "toolbar" }, newName, el("button", { type: "button", class: "primary", text: "Add person", onclick: addPerson })));

    const activity = el("section", { class: "card" }, el("h2", { text: "Sign-in activity" }),
      log.locked
        ? el("p", { class: "error", role: "alert", text: "Sign-in is locked. Unlock it from the NAS: docker exec -it homefinance python -m app.cli unlock" })
        : el("p", { class: "muted", text: `${log.failed_streak} of ${log.max_failed_logins} failed attempts in a row. At ${log.max_failed_logins}, new sign-ins are locked until you unlock them on the NAS. A successful sign-in resets the count.` }),
      log.events.length === 0 && el("p", { class: "empty", text: "Nothing recorded yet." }),
      log.events.map((e) => {
        const [label, bad] = LOGIN_RESULTS[e.result] || [e.result, false];
        const detail = [new Date(e.at).toLocaleString("en-CA"), e.ip && `from ${e.ip}`, e.origin, e.method === "recovery" && "recovery code", e.device_name].filter(Boolean).join(" \u00B7 ");
        return el("div", { class: "row" }, el("div", { class: "grow" },
          el("div", {}, el("strong", { class: bad ? "neg" : "", text: label }), e.count > 1 && el("span", { class: "badge", text: `\u00D7${e.count}` })),
          el("div", { class: "muted", text: detail })));
      }));

    const sections = { banks: [banks], accounts: [accountsCard], people: [peopleCard], rules: [rulesCard], appearance: [appearanceCard()], devices: [devices], log: [activity] };
    const tabs = el("div", { class: "seg wide", role: "group", "aria-label": "Settings sections" },
      SETTINGS_TABS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === settingsTab), text: label,
        onclick: () => { settingsTab = key; show(); } })));
    function show() {
      for (const [i, [key]] of SETTINGS_TABS.entries()) tabs.children[i].setAttribute("aria-pressed", String(key === settingsTab));
      view().replaceChildren(el("div", { class: "toolbar" }, tabs), ...sections[settingsTab]);
    }
    show();
  }

  // ---------------------------------------------------------------- shell: routes, sign-in

  const cleanups = [];
  const PAGES = {
    overview: ["Overview", "Net worth, spending and accounts"],
    transactions: ["Transactions", "Search, filter and re-categorize"],
    merchants: ["Merchants", "Where your money goes, over time"],
    budgets: ["Budgets", "Limits and spending by month"],
    money: ["Money Management", "Credit card payments and due dates"],
    investments: ["Investments", "Positions at live prices"],
    settings: ["Settings", "Banks, accounts, rules, appearance and sign-in"],
  };
  const ROUTES = { overview: viewOverview, transactions: viewTransactions, merchants: viewMerchants, budgets: viewBudgets, money: viewMoney, investments: viewInvestments, settings: viewSettings };

  // The footer's "last synced" and the reconnect banner come from the banks list, on every screen.
  async function refreshStatus() {
    try {
      const items = await api("/api/items");
      const newest = items.map((i) => i.last_synced_at).filter(Boolean).sort((a, b) => new Date(a) - new Date(b)).pop();
      $("foot-sync").textContent = items.length === 0 ? "No banks linked yet" : newest ? `Last synced ${ago(newest)}` : "Waiting for the first sync\u2026";
      setBanner(items.filter((i) => i.needs_reauth).map((i) => i.institution || "Unknown bank"));
    } catch (err) {
      if (err instanceof SignedOut) showLogin();
    }
  }

  async function showEnvironment() {
    try {
      const { plaid_env: env } = await (await plainFetch("/health")).json();
      const badge = $("env-badge");
      badge.textContent = env === "production" ? "Live banks" : "Sandbox";
      badge.title = env === "production" ? "Connected to real banks through Plaid" : "Plaid's sandbox: fake banks and test data";
      badge.className = env === "production" ? "env prod" : "env";
      badge.hidden = false;
    } catch (_) { /* the badge is only a convenience */ }
  }

  function setBanner(institutions) {
    const banner = $("banner");
    if (!institutions || institutions.length === 0) { banner.hidden = true; return; }
    banner.replaceChildren(`${institutions.join(", ")} needs to be reconnected. `,
      el("a", { href: "#/settings?tab=banks", text: "Reconnect it in Settings" }));
    banner.hidden = false;
  }

  async function route() {
    if (!token && $("shell").hidden) return;
    while (cleanups.length) cleanups.pop()();
    syncPolls = 0;
    const [path, query] = (location.hash.slice(1) || "/overview").split("?");
    const name = path.replace(/^\//, "") in ROUTES ? path.replace(/^\//, "") : "overview";
    for (const a of document.querySelectorAll("#tabs a, #menu-list a")) {
      if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    menuButton.classList.toggle("current", name === "settings");
    const [title, subtitle] = PAGES[name];
    $("page-title").textContent = title;
    $("page-sub").textContent = subtitle;
    document.title = `${title} \u00B7 HomeFinance`;
    const id = ++navId;
    view().replaceChildren(el("p", { class: "empty", text: "Loading…" }));
    try {
      await loadPeople();
      if (id !== navId) return;
      await ROUTES[name](id, new URLSearchParams(query || ""));
      refreshStatus();
    } catch (err) {
      if (err instanceof SignedOut) showLogin();
      else if (id === navId) view().replaceChildren(el("p", { class: "empty error", role: "alert", text: err.message }),
        el("p", { class: "empty" }, el("button", { type: "button", text: "Try again", onclick: route })));
    }
  }

  function showLogin(message = "") {
    token = null;
    closeSheet();
    $("shell").hidden = true;
    $("login").hidden = false;
    $("login-message").textContent = message;
    view().replaceChildren();
    $("banner").hidden = true;
    $("alert").hidden = true;
    const user = document.querySelector("#login-form [name=username]");
    if (user) user.focus();
  }

  function showShell() {
    $("login").hidden = true;
    $("shell").hidden = false;
    showEnvironment();
    route();
  }

  async function signOut() {
    try {
      const response = await plainFetch("/web/logout", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } });
      if (!response.ok) throw new Error(`Could not sign out (${response.status}). You are still signed in.`);
    } catch (err) {
      // Stay signed in: pretending otherwise would leave a live cookie that signs you straight back in.
      toast(err.message, true);
      return;
    }
    budgetMonth = null;
    showLogin("Signed out.");
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const button = event.target.querySelector("button");
    button.disabled = true;
    $("login-message").textContent = "";
    try {
      const response = await plainFetch("/web/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "HomeFinance" },
        body: JSON.stringify({ username: form.get("username"), password: form.get("password"), code: form.get("code") }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        $("login-message").textContent = response.status === 429
          ? "Too many attempts. Wait a few minutes and try again." : (typeof data.detail === "string" ? data.detail : "Sign-in failed.");
        return;
      }
      takeTokens(data);
      event.target.reset();
      showShell();
    } catch (err) {
      $("login-message").textContent = err.message;
    } finally { button.disabled = false; }
  });

  for (const button of document.querySelectorAll(".refresh-btn")) button.addEventListener("click", route);
  // The cog opens a small menu: Settings and Sign out.
  const menuButton = $("menu-button");
  const menuList = $("menu-list");
  function setMenu(open) {
    menuList.hidden = !open;
    menuButton.setAttribute("aria-expanded", String(open));
    if (open) menuList.querySelector("[role=menuitem]").focus();
  }
  menuButton.addEventListener("click", () => setMenu(menuList.hidden));
  menuList.addEventListener("click", () => setMenu(false));
  document.addEventListener("click", (e) => { if (!menuList.hidden && !e.target.closest("#account-menu")) setMenu(false); });
  document.addEventListener("keydown", (e) => {
    if (menuList.hidden) return;
    if (e.key === "Escape") { setMenu(false); menuButton.focus(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const items = [...menuList.querySelectorAll("[role=menuitem]")];
      const at = items.indexOf(document.activeElement);
      items[(at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    }
  });
  $("signout").addEventListener("click", signOut);
  $("viewer").addEventListener("change", (e) => { viewer = e.target.value === "" ? null : Number(e.target.value); route(); });
  window.addEventListener("hashchange", route);
  // Hide the page contents whenever the tab is not visible (task switchers, screenshots of other windows).
  document.addEventListener("visibilitychange", () => { $("cover").hidden = !document.hidden; });

  (async () => {
    try {
      if (await renewOnce()) showShell(); else showLogin();
    } catch (err) {
      showLogin(err.message);
    }
  })();
})();
