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

  // The phone layout does not zoom: no pinch or double-tap resizing. Phones' browsers may still allow a
  // pinch (iPhone Safari ignores this by design, for accessibility); Android Chrome obeys it. Desktop is untouched.
  if (window.matchMedia("(max-width: 760px) and (pointer: coarse)").matches) {
    document.querySelector("meta[name=viewport]")?.setAttribute("content", "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover");
  }

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

  // On a phone the charts are drawn 360 wide (about their real width) with larger text, instead of 640 wide and
  // shrunk to half size, which left the axis labels too small to read. How many labels fit along an axis is worked
  // out from the text size.
  // (600px matches the phone breakpoint in app.css; keep the two together.)
  const PHONE_CHARTS = window.matchMedia ? window.matchMedia("(max-width: 600px)") : { matches: false };
  const chartW = () => (PHONE_CHARTS.matches ? 360 : 640);
  const everyNth = (count, room, labelWidth) => Math.max(1, Math.ceil((labelWidth * count) / room));  // show every nth of `count` labels

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
  const BUILT_IN_PRIMARIES = KNOWN_CATEGORIES.filter((c) => c !== "GROCERIES");
  let PRIMARIES = BUILT_IN_PRIMARIES;  // plus the top-level categories you added in Settings
  let customCategories = [];           // [{code, parent, transactions, budgets, rules}]
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
  let sessionMax = 8 * 3600;  // likewise
  let idleSeconds = 600;  // the server tells the page its real limit with every sign-in
  let sessionEnds = Infinity;
  let signOutReason = "";
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
    if (data.idle_timeout) idleSeconds = data.idle_timeout;
    if (data.session_max) sessionMax = data.session_max;
    if (data.session_expires_in !== undefined) sessionEnds = Date.now() + data.session_expires_in * 1000;
  }

  async function renew() {
    const response = await plainFetch("/web/refresh", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } });
    if (response.status === 401) {
      const answer = await response.json().catch(() => ({}));
      signOutReason = /inactivity|time limit/.test(answer.detail || "") ? answer.detail : "";  // the server says why, when it was a timeout
      token = null;
      return false;
    }
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

  // A file download with the bearer token (a plain link cannot send it). Saves it under `filename`.
  async function downloadFile(path, filename) {
    if (!token || Date.now() >= tokenExpiry) { if (!(await renewOnce())) throw new SignedOut(); }
    let response = await plainFetch(path, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(120000) });
    if (response.status === 401) {
      if (!(await renewOnce())) throw new SignedOut();
      response = await plainFetch(path, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(120000) });
    }
    if (!response.ok) throw new HttpError(response.status, `Download failed (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    const link = el("a", { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  // ---------------------------------------------------------------- charts

  // Net worth over time with a line each for assets, liabilities and investments. The key below the
  // chart doubles as switches (at least one line stays on) and shows each figure for the day you point at.
  const NW_SERIES = [["net_worth", "Net worth", "s-net"], ["assets", "Assets", "s-assets"], ["liabilities", "Liabilities", "s-liab"], ["investments", "Investments", "s-inv"]];
  const nwHidden = new Set();
  function netWorthChart(rows, currency) {
    const wrap = el("div");
    if (rows.length < 2) return el("p", { class: "empty", text: "Not enough history yet. The chart fills in as days of data are collected." });
    const W = chartW(), phone = W < 640, H = phone ? 230 : 240, L = 62, R = 12, T = 12, B = phone ? 30 : 26;
    const x = (i) => L + (i / (rows.length - 1)) * (W - L - R);
    // Days before the first recorded one are estimated from transactions and market prices: drawn fainter.
    const firstRecorded = rows.findIndex((r) => !r.estimated);
    const split = firstRecorded === -1 ? rows.length - 1 : firstRecorded;
    const hasEstimates = rows.some((r) => r.estimated);
    const keyButtons = new Map();
    const caption = el("p", { class: "muted" });
    const estimateNote = hasEstimates && el("p", { class: "muted small", text: `Before ${shortDate(rows[split].date)} these are estimates, worked back from your transactions and market prices.` });
    const key = el("div", { class: "nw-key", role: "group", "aria-label": "Lines on the chart" });
    for (const [field, label, cls] of NW_SERIES) {
      const value = el("span", { class: "nw-value" });
      const button = el("button", { type: "button", class: `nw-chip ${cls}`, "aria-pressed": String(!nwHidden.has(field)), title: `Show or hide ${label.toLowerCase()}`,
        onclick: () => {
          if (nwHidden.has(field)) nwHidden.delete(field);
          else if (NW_SERIES.length - nwHidden.size > 1) nwHidden.add(field);
          draw();
        } },
        svg("svg", { class: "nw-swatch", width: 22, height: 10, "aria-hidden": "true" }, svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, class: `line ${cls}` })),
        el("span", { class: "nw-label", text: label }), value);
      keyButtons.set(field, { button, value });
      key.append(button);
    }
    let chart = null;
    let current = rows.length - 1;
    function show(i) {
      current = i;
      for (const [field] of NW_SERIES) keyButtons.get(field).value.textContent = money(rows[i][field], currency);
      caption.textContent = dayLabel(rows[i].date) + (rows[i].estimated ? " \u00B7 estimate" : "");
      if (!chart) return;
      for (const dot of chart.querySelectorAll("circle.dot")) {
        dot.setAttribute("cx", x(i));
        dot.setAttribute("cy", dot.dataset.field && chart._y(rows[i][dot.dataset.field]));
      }
    }
    function draw() {
      for (const [field] of NW_SERIES) keyButtons.get(field).button.setAttribute("aria-pressed", String(!nwHidden.has(field)));
      const shown = NW_SERIES.filter(([field]) => !nwHidden.has(field));
      const values = shown.flatMap(([field]) => rows.map((r) => r[field]));
      let lo = Math.min(...values), hi = Math.max(...values);
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.08;
      lo -= pad; hi += pad;
      const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
      const next = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Net worth, assets, liabilities and investments over time" });
      next._y = y;
      for (let i = 0; i <= 3; i++) {
        const v = lo + ((hi - lo) * i) / 3;
        next.append(svg("line", { class: "gl", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
        const label = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" });
        label.textContent = money(v, currency, { notation: "compact", maximumFractionDigits: 1 });
        next.append(label);
      }
      if (lo < 0 && hi > 0) next.append(svg("line", { class: "gl zero", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
      const draw1 = (field, from, to) => rows.slice(from, to + 1).map((r, n) => `${n ? "L" : "M"}${x(from + n).toFixed(1)},${y(r[field]).toFixed(1)}`).join(" ");
      if (hasEstimates && split > 0) next.append(svg("line", { class: "gl recorded-from", x1: x(split), x2: x(split), y1: T, y2: H - B }));
      for (const [field, , cls] of shown) {
        const path = draw1(field, 0, rows.length - 1);
        if (field === "net_worth") next.append(svg("path", { class: `area ${cls}`, d: `${path} L${x(rows.length - 1)},${y(Math.max(lo, Math.min(0, hi)))} L${L},${y(Math.max(lo, Math.min(0, hi)))} Z` }));
        if (hasEstimates && split > 0) {
          next.append(svg("path", { class: `line ${cls} estimated`, d: draw1(field, 0, split) }));
          next.append(svg("path", { class: `line ${cls}`, d: draw1(field, split, rows.length - 1) }));
        } else {
          next.append(svg("path", { class: `line ${cls}`, d: path }));
        }
      }
      for (const i of [0, Math.floor((rows.length - 1) / 2), rows.length - 1]) {
        const label = svg("text", { x: x(i), y: H - 7, "text-anchor": i === 0 ? "start" : i === rows.length - 1 ? "end" : "middle" });
        label.textContent = parseDay(rows[i].date).toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "2-digit" });
        next.append(label);
      }
      for (const [field, , cls] of shown) next.append(svg("circle", { r: 3.5, class: `dot ${cls}`, "data-field": field }));
      next.addEventListener("pointermove", (event) => {
        const box = next.getBoundingClientRect();
        const px = ((event.clientX - box.left) / box.width) * W;
        show(Math.max(0, Math.min(rows.length - 1, Math.round(((px - L) / (W - L - R)) * (rows.length - 1)))));
      });
      next.addEventListener("pointerleave", () => show(rows.length - 1));
      if (chart) chart.replaceWith(next); else wrap.append(next);
      chart = next;
      show(current);
    }
    draw();
    wrap.append(caption, key);
    if (estimateNote) wrap.append(estimateNote);
    return wrap;
  }

  function barChart(rows, currency) {
    const W = chartW(), phone = W < 640, H = phone ? 210 : 200, L = 62, R = 8, T = 10, B = phone ? 28 : 24;
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
    const nth = everyNth(rows.length, W - L - R, phone ? 34 : 28);
    rows.forEach((row, i) => {
      const h = Math.max(0, (row.total / max) * (H - T - B));
      const bar = svg("rect", { class: "bar", x: L + i * slot + slot * 0.18, y: H - B - h, width: slot * 0.64, height: h, rx: 3 });
      const tip = svg("title");
      tip.textContent = `${monthLabel(row.month)}: ${money(row.total, currency)}`;
      bar.append(tip);
      chart.append(bar);
      if (i % nth === 0) {
        const label = svg("text", { x: L + i * slot + slot / 2, y: H - 8, "text-anchor": "middle" });
        label.textContent = shortMonth(row.month);
        chart.append(label);
      }
    });
    return chart;
  }

  function progress(percent, plain = false) {
    const cls = plain ? "progress plain" : percent >= 100 ? "progress over" : percent >= 80 ? "progress warn" : "progress";
    return el("div", { class: cls, role: "progressbar", "aria-valuenow": Math.round(percent), "aria-valuemin": 0, "aria-valuemax": 100 },
      (() => { const bar = document.createElement("i"); bar.style.width = `${Math.min(100, Math.max(0, percent))}%`; return bar; })());
  }


  // A budgeted category's bar: the limit is the full length while spending is under it; past it the bar keeps going,
  // red beyond the limit, with a red line where the limit was.
  function budgetBar(spent, limit) {
    const scale = Math.max(limit, spent, 0.01);
    const over = spent > limit;
    const bar = document.createElement("i");
    bar.style.width = `${Math.min(100, Math.max(0, spent) / scale * 100)}%`;
    const track = el("div", { class: `progress budgeted${over ? " over" : spent >= limit * 0.8 ? " warn" : ""}`, role: "progressbar",
      "aria-valuenow": Math.round(spent / Math.max(limit, 0.01) * 100), "aria-valuemin": 0, "aria-valuemax": 100,
      "aria-label": over ? "Over budget" : "Within budget" }, bar);
    if (over) {
      track.style.setProperty("--limit", `${limit / scale * 100}%`);
      const line = document.createElement("b");
      line.className = "limit-line";
      track.append(line);
    }
    return track;
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
  // The kinds a loan can be set to (the server owns the list). Asked for once.
  let loanTypeList = null;
  async function loanTypes() {
    if (loanTypeList) return loanTypeList;
    try { loanTypeList = await api("/api/loan-types"); } catch (err) { if (err instanceof SignedOut) throw err; return []; }  // a failed fetch is not remembered
    return loanTypeList;
  }

  // What kind of loan an account is: Automatic goes by the bank's own label, or pick one (mortgage, student, car ...).
  function loanKindControl(a, types) {
    const label = (kind) => (types.find((t) => t.kind === kind) || { label: kind }).label;
    const select = el("select", { class: "loan-kind", "aria-label": `Kind of loan for ${a.name}` },
      el("option", { value: "", text: a.loan_type ? `Automatic (${label(a.loan_type)})` : "Automatic" }),
      types.map((t) => el("option", { value: t.kind, text: t.label })));
    select.value = a.loan_kind || "";
    select.addEventListener("change", async () => {
      select.disabled = true;
      try {
        await api(`/api/accounts/${encodeURIComponent(a.id)}`, { method: "PATCH", body: { loan_kind: select.value || null } });
        toast("Saved.");
        route();
      } catch (err) {
        if (err instanceof SignedOut) showLogin(); else { toast(err.message, true); select.value = a.loan_kind || ""; select.disabled = false; }
      }
    });
    return el("label", { class: "loan-kind-row muted" }, el("span", { text: "Kind of loan " }), select);
  }

  function accountSection(kind, label, iconId, rows, itemsById, loanKinds = []) {
    const credit = kind === "credit";
    const showOwner = people.length > 0;
    const open = (a) => { location.hash = `#/transactions?account=${encodeURIComponent(a.id)}`; };
    // The bank is its own column: one cell spans all of that bank's accounts in this section, so it
    // is shown once. Banks are ordered by what they hold, and each bank's accounts the same way.
    const headers = [["Bank", "col-bankname"], ["Account", "col-account"], showOwner && ["Owner", "col-acct-owner"],
      credit && ["Limit", "num col-extra"], credit && ["Available", "num col-extra"], credit && ["Used", "num col-extra"],
      kind === "depository" && ["Available", "num col-extra"],
      [kind === "investment" ? "Value" : credit || kind === "loan" ? "Owed" : "Balance", "num col-balance", true]].filter(Boolean);
    const size = (a) => Math.abs(a.current_balance || 0);
    const banks = new Map();
    for (const a of rows) {
      const key = a.item_id || a.institution || "";
      if (!banks.has(key)) banks.set(key, []);
      banks.get(key).push(a);
    }
    const groups = [...banks.values()]
      .map((accounts) => accounts.slice().sort((x, y) => size(y) - size(x)))
      .sort((x, y) => y.reduce((sum, a) => sum + size(a), 0) - x.reduce((sum, a) => sum + size(a), 0));
    const body = groups.flatMap((accounts) => accounts.map((a, index) => {
      const item = itemsById.get(a.item_id);
      const available = credit && a.credit_limit != null
        ? (a.available_balance ?? (a.current_balance != null ? a.credit_limit - a.current_balance : null)) : null;
      const used = credit && a.credit_limit > 0 && a.current_balance != null ? Math.round((a.current_balance / a.credit_limit) * 100) : null;
      const cells = [
        index === 0 && el("td", { class: "col-bankname shared", rowspan: accounts.length },
          el("div", { class: "bank-cell", title: a.institution || (item && item.institution) || "Bank" }, bankLogo(item, a.institution, 28), el("strong", { text: a.institution || (item && item.institution) || "Bank" }))),
        el("td", { class: "col-account" },
          el("a", { href: `#/transactions?account=${encodeURIComponent(a.id)}`, class: "acct-name", text: a.name }),
          showOwner && el("div", { class: "muted acct-owner", text: ownerText(a) || "" }),
          kind === "loan" && loanKinds.length > 0 && loanKindControl(a, loanKinds)),
        // On a computer the owner has a column of its own; on a phone it stays as the small line under the account name.
        showOwner && el("td", { class: "col-acct-owner", text: ownerText(a) || "" }),
        credit && el("td", { class: "num col-extra", text: a.credit_limit != null ? money(a.credit_limit, a.currency) : "\u2014" }),
        credit && el("td", { class: "num col-extra", text: available != null ? money(available, a.currency) : "\u2014" }),
        credit && el("td", { class: `num col-extra${used != null && used >= 80 ? " neg" : ""}`, text: used != null ? `${used}%` : "\u2014" }),
        kind === "depository" && el("td", { class: "num col-extra", text: a.available_balance != null ? money(a.available_balance, a.currency) : "\u2014" }),
        el("td", { class: "num col-balance" }, a.current_balance == null ? "\u2014" : money(a.current_balance, a.currency)),
      ].filter(Boolean);
      return el("tr", { class: `click${index === 0 ? " bank-first" : ""}`, tabindex: 0,
        onclick: (e) => { if (!e.target.closest("a, td.shared, label")) open(a); },
        onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) open(a); } }, cells);
    }));
    return el("section", { class: `card acct-section ${kind}` },
      el("div", { class: "section-head" },
        el("div", { class: "section-icon", "aria-hidden": "true" }, iconSvg(iconId, 22)),
        el("div", { class: "grow" }, el("h2", { text: label })),
        el("div", { class: "head-total", text: `Total: ${sectionTotals(rows, kind)}` })),
      el("div", { class: "table-wrap" }, el("table", { class: "acct-table bank-grouped" },
        el("thead", {}, el("tr", {}, headers.map(([text, cls, unlabelled]) => el("th", { scope: "col", class: cls },
          // The amounts speak for themselves (and the header says "owed" for credit and loans), so this column's title is not shown; screen readers still hear it.
          unlabelled ? el("span", { class: "sr", text }) : text)))),
        el("tbody", {}, body))));
  }

  // A line under the page title in a person's view; empty for the household.
  function viewerNote(householdOnly = false) {
    if (viewer == null || !householdOnly) return document.createDocumentFragment();  // nothing to show, and nothing printed
    const name = personName(viewer) || "this person";
    return el("p", { class: "viewer-note", text: `This page covers the whole household, not just ${name}.` });
  }
  let peopleLoaded = false;
  const openBanks = new Set();  // Settings > Banks: which banks are unfolded
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
    try {
      const custom = await api("/api/categories/custom");
      customCategories = custom.categories;
      PRIMARIES = [...new Set([...BUILT_IN_PRIMARIES, ...custom.primaries])];
    } catch (err) { if (err instanceof SignedOut) throw err; }
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

  // The same picker with a first choice for "no saved category" (value() is then null).
  function optionalCategoryPicker(choices, current) {
    const picker = categoryPicker(current && !choices.includes(current) ? [...choices, current] : choices, current);
    const select = picker.node.firstChild;
    select.prepend(el("option", { value: "", text: "Automatic" }));
    if (!current) select.value = "";
    return { node: picker.node, value: () => (select.value === "" ? null : picker.value() || null) };
  }

  // A switch for "money coming in from this merchant counts as income". Returns { node, value() }.
  function incomeSwitch(on) {
    const box = el("input", { type: "checkbox", role: "switch", checked: !!on, "aria-label": "Count money coming in as income" });
    return {
      node: el("label", { class: "switch-row" }, el("span", { class: "switch" }, box, el("span", { class: "track", "aria-hidden": "true" })),
        el("span", {}, "Count money coming in as income")),
      value: () => box.checked,
    };
  }
  const INCOME_NOTE = "Off keeps it automatic: money back from a purchase reduces what you spent. On makes every deposit from this merchant income, such as a paycheque.";

  // The X in the top right corner of a popup (shown on a computer; a phone has the Close button and swipe room instead).
  const dialogX = (close) => el("button", { type: "button", class: "dialog-x", "aria-label": "Close", title: "Close", text: "\u00D7", onclick: close });

  function openSheet(...content) {
    const sheet = $("sheet");
    const nodes = content.flat(Infinity).filter((node) => node != null && node !== false);
    // The title and the X sit in a header that never scrolls, and a trailing button row (Close, Save...) is pinned under the
    // scrolling part, so neither has to be scrolled to and the scroll bar never runs up behind the title.
    const title = nodes.length > 1 && nodes[0].tagName === "H2" ? nodes.shift() : null;
    const last = nodes[nodes.length - 1];
    const actions = nodes.length > 1 && last.classList && last.classList.contains("actions") ? nodes.pop() : null;
    sheet.replaceChildren(
      el("div", { class: "sheet-head" }, title || el("span"), dialogX(closeSheet)),
      el("div", { class: "sheet-body" }, ...nodes),
      ...(actions ? [actions] : []));
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
          el("button", { type: "button", class: "secondary", text: cancelText, onclick: close })),
        dialogX(close)));
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

  // ---------------------------------------------------------------- inactivity timeout

  // Like a banking site: with no tap, key or scroll for the server's idle limit you are signed out, with a
  // "Still there?" box for the last minute. The server enforces the same limits, so this is only the courtesy.
  const IDLE_WARNING_SECONDS = 60;
  const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart", "scroll"];
  let lastActive = Date.now();
  let lastPing = 0;
  let idleTimer = null;
  let pendingPing = null;
  let idleHold = false;  // true while Plaid's window is open (it is in another frame and cannot be seen touching the page)

  // true: recorded. false: the server says the session is over (401). A network or server error throws instead,
  // so one failed ping never signs you out; the page's own clock still ends an idle session.
  async function activityPing() {
    if (!token || Date.now() >= tokenExpiry) { if (!(await renewOnce())) return false; }
    const send = () => plainFetch("/web/activity", { method: "POST", headers: { Authorization: `Bearer ${token}`, "X-Requested-With": "HomeFinance" } });
    let response = await send();
    if (response.status === 401) {
      if (!(await renewOnce())) return false;
      response = await send();
      if (response.status === 401) return false;
    }
    if (!response.ok) throw new Error(`Could not tell the server you are here (${response.status})`);
    return true;
  }

  function reportActivity() {
    clearTimeout(pendingPing);
    pendingPing = null;
    lastPing = Date.now();
    activityPing().then((ok) => { if (!ok) idleSignOut(); }, () => {});
  }

  function noteActivity() {
    const box = $("idle");
    if (box.open) return;  // once the box is up, only its buttons answer it
    const now = Date.now();
    lastActive = now;
    if (now - lastPing > 60000) reportActivity();
    else if (pendingPing === null) pendingPing = setTimeout(reportActivity, 60000 - (now - lastPing));  // the last tap of a burst is reported too
  }

  function stopIdleWatch() {
    clearInterval(idleTimer);
    clearTimeout(pendingPing);
    pendingPing = null;
    idleTimer = null;
    for (const name of ACTIVITY_EVENTS) document.removeEventListener(name, noteActivity, true);
    document.removeEventListener("visibilitychange", checkIdle);
    const box = $("idle");
    if (box.open) box.close();
    const setup = $("passkey-setup");
    if (setup.open) setup.close();  // its close handler drops the password it was holding
  }

  // 600 -> "10 minutes", 3600 -> "1 hour", the same wording the server uses.
  function spanText(seconds) {
    if (seconds % 3600 === 0) { const h = seconds / 3600; return `${h} hour${h === 1 ? "" : "s"}`; }
    const m = Math.max(1, Math.round(seconds / 60));
    return `${m} minute${m === 1 ? "" : "s"}`;
  }

  function startIdleWatch() {
    stopIdleWatch();
    lastActive = lastPing = Date.now();
    for (const name of ACTIVITY_EVENTS) document.addEventListener(name, noteActivity, { capture: true, passive: true });
    document.addEventListener("visibilitychange", checkIdle);
    idleTimer = setInterval(checkIdle, 1000);
  }

  async function idleSignOut(reason = "") {
    if (idleTimer === null) return;  // already signed out
    stopIdleWatch();
    try { await plainFetch("/web/logout", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } }); } catch (_) { /* the server expires it too */ }
    budgetMonth = null;
    showLogin(reason || signOutReason || `You were signed out after ${spanText(idleSeconds)} of inactivity`);  // the server's own reason wins
  }

  function checkIdle() {
    if (idleTimer === null) return;
    if (Date.now() >= sessionEnds) { idleSignOut(`You were signed out after reaching the ${spanText(sessionMax)} time limit for a session`); return; }
    if (idleHold) {
      lastActive = Date.now();
      if (Date.now() - lastPing > 60000) reportActivity();  // the server's clock has to hear about it too
    }
    const left = idleSeconds - (Date.now() - lastActive) / 1000;
    if (left <= 0) { idleSignOut(); return; }
    if (left > IDLE_WARNING_SECONDS) return;
    const box = $("idle");
    if (!box.open) {
      const count = el("span", { class: "idle-count", id: "idle-count" });
      box.replaceChildren(el("div", { class: "idle" },
        el("h2", { id: "idle-title", text: "Still there?" }),
        el("p", {}, "You will be signed out in ", count, " seconds because there has been no activity."),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Stay signed in", onclick: async () => {
            let ok;
            try { ok = await activityPing(); } catch (_) {
              box.querySelector(".idle-error").textContent = "Could not reach the server. Try again.";  // the countdown carries on
              return;
            }
            if (!ok) { idleSignOut(); return; }
            lastActive = lastPing = Date.now();
            box.close();
          } }),
          el("button", { type: "button", class: "secondary", text: "Sign out now", onclick: () => idleSignOut("Signed out.") })),
        el("p", { class: "error idle-error", role: "alert" })));
      box.oncancel = (event) => event.preventDefault();  // Escape does not count as an answer
      box.showModal();
    }
    const counter = $("idle-count");
    if (counter) counter.textContent = String(Math.ceil(left));
  }

  // ---------------------------------------------------------------- passkeys (Face ID)

  const b64url = {
    encode: (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
    decode: (text) => {
      const padded = text.replace(/-/g, "+").replace(/_/g, "/");
      return Uint8Array.from(atob(padded + "=".repeat((4 - (padded.length % 4)) % 4)), (c) => c.charCodeAt(0)).buffer;
    },
  };
  const passkeysSupported = () => Boolean(window.PublicKeyCredential && navigator.credentials);
  // Browsers do not let a page ask whether a passkey exists, so this browser remembers that one was added or used
  // here (see theme.js). The Face ID button leads the sign-in page only then; it is forgotten when your list is empty.
  const rememberPasskey = (yes) => window.HFTheme.set({ passkey: yes ? "yes" : "no" });
  function showPasskeyFirst() {
    const on = passkeysSupported() && window.HFTheme.get().passkey === "yes";
    $("passkey-first").hidden = !on;
    $("password-passkey-submit").hidden = !(passkeysSupported() && !on);
    document.querySelector("#login-form").classList.toggle("passkey-first", on);
    if (on) prefetchLoginOptions().catch(() => {});  // ready before the tap
  }
  // A failed prefetch is silent on purpose: if the server is down the error shows when you tap.
  setInterval(() => { if (!$("login").hidden && !$("passkey-first").hidden) prefetchLoginOptions().catch(() => {}); }, 30000);

  // After signing in with all three details, offer to make the passkey on the spot. The password just typed is
  // used once for it (adding a passkey asks for it) and then dropped; it is never stored.
  function offerPasskeySetup(password) {
    const box = $("passkey-setup");
    let pending = password;
    const message = el("p", { class: "error", role: "alert" });
    const close = () => { pending = ""; if (box.open) box.close(); };
    box.addEventListener("close", () => { pending = ""; }, { once: true });  // also when something else closes it (sign-out)
    prefetchRegisterOptions().catch(() => {});  // so the tap goes straight to Face ID
    box.replaceChildren(el("div", { class: "setup" },
      el("h2", { id: "passkey-setup-title", text: "Set up a passkey?" }),
      el("p", {}, "Create a passkey on this device. Next time you can sign in with it, with no password or code."),
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Create passkey", onclick: async (event) => {
          message.textContent = "";
          event.target.disabled = true;
          try { await addPasskey(guessDevice(), pending); toast("Passkey is set up"); close(); }
          catch (err) {
            if (err instanceof PasskeyExists) { toast("This device already has a passkey. Passkey sign-in is ready."); close(); return; }
            message.textContent = err.message;
            event.target.disabled = false;
          }
        } }),
        el("button", { type: "button", class: "secondary", text: "Not now", onclick: close })),
      message));
    box.oncancel = close;
    box.showModal();
  }

  // Safari only lets a page ask for Face ID right after your tap. So the one-time challenge is fetched ahead of
  // time (fresh within 90 s, the server keeps it for two minutes) and the tap goes straight to Face ID.
  function cached(slot, fetcher) {
    if (slot.value && Date.now() - slot.value.at < 90000) return slot.value.promise;
    const promise = fetcher();
    slot.value = { at: Date.now(), promise };
    promise.catch(() => { if (slot.value?.promise === promise) slot.value = null; });
    return promise;
  }
  const loginSlot = { value: null };
  const registerSlot = { value: null };
  const prefetchLoginOptions = () => cached(loginSlot, async () => {
    const asked = await plainFetch("/web/passkey/options", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } });
    const answer = await asked.json().catch(() => ({}));
    if (!asked.ok) throw new Error(typeof answer.detail === "string" ? answer.detail : "Could not start the passkey sign-in.");
    return answer;
  });
  const prefetchRegisterOptions = () => cached(registerSlot, () => api("/api/passkeys/options", { method: "POST" }));

  async function passkeySignIn() {
    const options = await prefetchLoginOptions();
    loginSlot.value = null;  // a challenge works once
    const key = options.publicKey;
    let credential;
    try {
      credential = await navigator.credentials.get({ publicKey: {
        challenge: b64url.decode(key.challenge), rpId: key.rpId, userVerification: key.userVerification, timeout: key.timeout, allowCredentials: [] } });
    } catch (err) {
      prefetchLoginOptions().catch(() => {});  // a fresh challenge is ready, so the next tap goes straight to Face ID
      throw new Error(err.name === "NotAllowedError"
        ? "Cancelled, or this device has no passkey for HomeFinance. Sign in with your password once, then add one in Settings \u2192 Security \u2192 Passkeys."
        : `Passkey sign-in failed (${err.name}).`);
    }
    const reply = credential.response;
    const response = await plainFetch("/web/passkey/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "HomeFinance" },
      body: JSON.stringify({
        handle: options.handle, credential_id: credential.id, client_data: b64url.encode(reply.clientDataJSON),
        authenticator_data: b64url.encode(reply.authenticatorData), signature: b64url.encode(reply.signature) }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(response.status === 429 ? "Too many attempts. Wait a few minutes and try again." : (typeof data.detail === "string" ? data.detail : "Sign-in failed."));
    }
    rememberPasskey(true);
    return data;
  }

  // The browser says InvalidStateError when this device already holds a passkey the server knows about (we list those
  // so a second one is not made). So one exists here: remember that, and the sign-in page will lead with Face ID.
  class PasskeyExists extends Error {
    constructor() { super("This device already has a passkey for HomeFinance, so passkey sign-in is ready. Sign out and tap Sign in with passkey."); }
  }

  async function addPasskey(name, password) {
    const options = await prefetchRegisterOptions();
    registerSlot.value = null;
    const key = options.publicKey;
    let credential;
    try {
      credential = await navigator.credentials.create({ publicKey: {
        rp: key.rp, user: { id: b64url.decode(key.user.id), name: key.user.name, displayName: key.user.displayName },
        challenge: b64url.decode(key.challenge), pubKeyCredParams: key.pubKeyCredParams, authenticatorSelection: key.authenticatorSelection,
        excludeCredentials: key.excludeCredentials.map((c) => ({ type: c.type, id: b64url.decode(c.id) })),
        attestation: key.attestation, timeout: key.timeout } });
    } catch (err) {
      prefetchRegisterOptions().catch(() => {});  // ready for the next try
      if (err.name === "InvalidStateError") { rememberPasskey(true); throw new PasskeyExists(); }
      throw new Error(err.name === "NotAllowedError" ? "Cancelled, or this device already has a passkey for HomeFinance." : `Could not make a passkey (${err.name}).`);
    }
    const added = await api("/api/passkeys", { method: "POST", body: {
      handle: options.handle, name, password, client_data: b64url.encode(credential.response.clientDataJSON),
      attestation: b64url.encode(credential.response.attestationObject) } });
    rememberPasskey(true);
    return added;
  }

  const guessDevice = () => (/iPhone/.test(navigator.userAgent) ? "iPhone" : /iPad/.test(navigator.userAgent) ? "iPad"
    : /Android/.test(navigator.userAgent) ? "Android phone" : /Macintosh/.test(navigator.userAgent) ? "Mac" : "This device");

  // ---------------------------------------------------------------- sign-in activity

  const LOGIN_RESULTS = {
    success: ["Signed in", false], bad_password: ["Wrong password", true], bad_code: ["Wrong authenticator code", true],
    unknown_user: ["Unknown username", true], rate_limited: ["Blocked: too many attempts", true],
    locked: ["Refused: sign-in is locked", true], lockout: ["Sign-in locked after too many failures", true],
    bad_passkey: ["Passkey refused", true], unlocked: ["Unlocked from the NAS", false], login_removed: ["A login was removed", false],
  };
  const notices = { banks: [], signin: null };  // what the cog counts
  const FAILED = new Set(["bad_password", "bad_code", "bad_passkey", "unknown_user", "lockout"]);

  // Failed sign-in attempts are one of the things the cog's count reports (see renderNotices).
  function setAlert(log) {
    const recent = log ? log.events.filter((e) => FAILED.has(e.result) && Date.now() - new Date(e.at).getTime() < 864e5).length : 0;
    notices.signin = !log || (!log.locked && recent === 0) ? null
      : log.locked ? "New sign-ins are locked after too many failed attempts"
      : `${recent} failed sign-in attempt${recent === 1 ? "" : "s"} in the last 24 hours`;
    renderNotices();
  }

  // ---------------------------------------------------------------- Overview

  let nwRange = "YTD";  // which of the chart ranges is chosen
  // Days back to the end of last year, so a year-to-date chart always has a starting point.
  const ytdDays = () => Math.floor((Date.now() - new Date(new Date().getFullYear(), 0, 1).getTime()) / 864e5) + 1;
  const NW_RANGES = [["1M", () => 30], ["3M", () => 90], ["YTD", ytdDays], ["All", () => 3650]];

  // Every account and its balance, by kind, with each bank's logo.
  async function viewAccounts(id) {
    const [accounts, items, loanKinds] = await Promise.all([api(scoped("/api/accounts?include_hidden=false")), api("/api/items").catch(() => []), loanTypes()]);
    if (id !== navId) return;
    const itemsById = new Map(items.map((i) => [i.id, i]));
    const known = new Set(["investment", "depository", "credit", "loan"]);
    const sections = ACCOUNT_SECTIONS
      .map(([kind, label, iconId]) => [kind, label, iconId, accounts.filter((a) => (kind === "other" ? !known.has(a.type) : a.type === kind))])
      .filter(([, , , rows]) => rows.length > 0)
      .map(([kind, label, iconId, rows]) => accountSection(kind, label, iconId, rows, itemsById, loanKinds));
    if (sections.length === 0) {
      sections.push(el("section", { class: "card" }, el("h2", { text: "Accounts" }),
        el("p", { class: "empty", text: "No accounts yet. Link a bank from Settings." })));
    }
    view().replaceChildren(viewerNote(), ...sections);
  }

  async function viewOverview(id) {
    const [summary, log] = await Promise.all([api(scoped("/api/summary")), api("/auth/login-log?limit=100").catch(() => null)]);
    if (id !== navId) return;
    setAlert(log);
    baseCurrency = summary.base_currency;
    setBanner(summary.items_needing_attention);
    const worth = summary.net_worth.find((n) => n.currency === baseCurrency) || summary.net_worth[0];
    const cur = worth ? worth.currency : baseCurrency;

    const chartBox = el("div");
    const drawChart = async () => {
      try {
        const rows = await api(scoped(`/api/networth?days=${NW_RANGES.find(([label]) => label === nwRange)[1]()}&currency=${encodeURIComponent(cur)}`));
        if (id === navId) // The last point is today's figures, the same ones shown above, so the two never disagree.
        if (worth && rows.length) rows[rows.length - 1] = { ...rows[rows.length - 1], net_worth: worth.net_worth, assets: worth.assets, liabilities: worth.liabilities, investments: worth.investments };
        chartBox.replaceChildren(netWorthChart(rows, cur));
      } catch (err) { if (err instanceof SignedOut) showLogin(); else chartBox.replaceChildren(el("p", { class: "error", text: err.message })); }
    };
    const seg = el("div", { class: "seg compact", role: "group", "aria-label": "Chart range" });
    const paint = () => seg.replaceChildren(...NW_RANGES.map(([label]) =>
      el("button", { type: "button", "aria-pressed": String(label === nwRange), text: label, onclick: () => { nwRange = label; paint(); drawChart(); } })));
    paint();

    // Assets and liabilities each with their parts, then the net worth total beneath both.
    const part = (label, value, sub) => el("div", { class: "nw-line" + (sub ? " sub" : "") }, el("span", { text: label }), el("span", { class: "num", text: money(value, cur) }));
    const w = worth || {};
    const breakdown = worth ? el("div", { class: "nw-breakdown" },
      part("Assets", w.assets),
      part("Cash", w.cash, true),
      part("Investments", w.investments, true),
      part("Liabilities", w.liabilities),
      ...(w.loans || []).map((l) => part(l.label, l.amount, true)),
      part("Credit card debt", w.credit_cards, true)) : null;

    const netCard = el("section", { class: "card" },
      el("h3", { text: "Net worth" }),
      breakdown,
      el("div", { class: "nw-total" }, el("span", { text: "Net worth" }), el("span", { class: "nw-sum", text: worth ? money(worth.net_worth, cur) : money(0, cur) })),
      summary.net_worth.filter((n) => n !== worth).map((n) => el("p", { class: "muted", text: `${n.currency}: ${money(n.net_worth, n.currency)}` })));
    // The history is a card of its own: the range choice, the chart, then the key to its lines beneath.
    const overTimeCard = el("section", { class: "card nw-chart-card" },
      el("div", { class: "nw-head" }, el("h3", { text: "Over time" }), seg),
      chartBox);

    view().replaceChildren(viewerNote(), netCard, overTimeCard);
    drawChart();
  }

  // The Spending page's Cash flow tab: income and expenses over time, with its own heading and controls.
  async function viewCashflowTab(id, params) {
    const box = el("div");
    view().replaceChildren(viewerNote(), box);
    await viewCashflow(id, params, box);
  }

  // The Spending page's first tab: this month's spending against the budgets, and the top categories.
  async function viewSpendingSummary(id) {
    const [summary, spending] = await Promise.all([
      api(scoped("/api/summary")),
      api(scoped("/api/spending")).catch(() => ({ categories: [], total: 0 }))]);  // no month = the current one, same as the summary
    if (id !== navId) return;
    baseCurrency = summary.base_currency;
    const worth = summary.net_worth.find((n) => n.currency === baseCurrency) || summary.net_worth[0];
    const cur = worth ? worth.currency : baseCurrency;
    const budgeted = summary.budgeted_this_month;
    const pct = budgeted > 0 ? (summary.spent_this_month / budgeted) * 100 : 0;
    // The over-budget warning sits on the category that is over (or under its parent, for a sub-category's
    // budget), so a category that is over shows in the list even when it is not one of the five biggest.
    const overBy = new Map(summary.over_budget.map((b) => [b.category, b.spent - b.monthly_limit]));
    const notesFor = (c) => [
      overBy.has(c.category) && `Over budget by ${money(overBy.get(c.category), cur)}`,
      ...(c.subcategories || []).filter((sub) => overBy.has(sub.category)).map((sub) => `${categoryLabel(sub.category)} over budget by ${money(overBy.get(sub.category), cur)}`)].filter(Boolean);
    const topRows = spending.categories.filter((c, n) => n < 5 || notesFor(c).length).map((c) => {
      const over = notesFor(c);
      return el("div", { class: "row" },
        el("div", { class: "icon", text: icon(c.category), "aria-hidden": "true" }),
        el("div", { class: "grow" }, el("div", { text: categoryLabel(c.category) }),
          over.map((note) => el("div", { class: "over-note", text: note })),
          progress(spending.total > 0 ? (Math.max(c.spent, 0) / spending.total) * 100 : 0, true)),
        el("div", { class: "amount", text: money(c.spent, cur) }));
    });
    const spendCard = el("section", { class: "card" },
      el("h3", { text: `Spent in ${monthLabel(summary.month)}` }),
      el("div", { class: "big", text: money(summary.spent_this_month, cur) }),
      budgeted > 0 ? [progress(pct), el("p", { class: "muted", text: `of ${money(budgeted, cur)} budgeted (${Math.round(pct)}%)` })]
        : el("p", { class: "muted", text: viewer != null ? "Budgets cover the whole household." : "No budgets set yet." }),
      spending.categories.length > 0 && [el("h3", { text: "Top categories" }), ...topRows]);

    view().replaceChildren(viewerNote(), spendCard);
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

    const search = el("input", { type: "search", placeholder: "Search", "aria-label": "Search transactions", title: "Search the merchant, bank text, category, account, date or amount", maxlength: 100 });
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
    const sourceLabel = { rule: "Set by a rule", merchant: "Set by its merchant", manual: "Changed by you", plaid: "From your bank (Plaid)" }[tx.category_source] || "From your bank (Plaid)";
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
  let currentMonth = null;  // this month (YYYY-MM): the Budgets month can go back, but never forward past it
  let budgetPopup = null;   // a category whose transactions popup should reopen once the page has reloaded (after a category change)

  async function viewBudgets(id) {
    if (!budgetMonth) {
      const summary = await api("/api/summary");
      budgetMonth = summary.month;
      currentMonth = summary.month;
      baseCurrency = summary.base_currency;
    }
    const thisMonth = new Date().toLocaleDateString("en-CA", { year: "numeric", month: "2-digit" });   // YYYY-MM in the device's time
    const latest = currentMonth && currentMonth > thisMonth ? currentMonth : thisMonth;
    const month = budgetMonth > latest ? latest : budgetMonth;
    const [budgets, spending, choices] = await Promise.all([
      api(`/api/budgets?month=${month}`), api(`/api/spending?month=${month}`), categoryChoices()]);
    if (id !== navId) return;
    const cur = budgets.currency;
    const reload = () => guard(viewBudgets(++navId));

    const nav = el("div", { class: "monthnav" },
      el("button", { type: "button", "aria-label": "Previous month", text: "‹", onclick: () => { budgetMonth = addMonths(month, -1); reload(); } }),
      el("strong", { text: monthLabel(month) }),
      el("button", { type: "button", "aria-label": "Next month", text: "›", disabled: month >= latest,
        title: month >= latest ? "Budgets can't be viewed for a future month" : "", onclick: () => { if (month < latest) { budgetMonth = addMonths(month, 1); reload(); } } }),
      el("button", { type: "button", class: "primary", text: "Add budget", onclick: () => budgetDialog(null) }));

    function budgetDialog(existing, preset = null) {
      const used = new Set(budgets.budgets.map((b) => b.category));
      const fixed = existing ? existing.category : preset;   // a row's own + button already knows the category
      const picker = fixed ? null : categoryPicker(choices.filter((c) => !used.has(c)), null);
      const limit = el("input", { type: "number", min: "0.01", step: "0.01", inputmode: "decimal", value: existing ? String(existing.monthly_limit) : "", "aria-label": "Monthly limit" });
      const message = el("p", { class: "error", role: "alert" });
      const save = async () => {
        const category = fixed || picker.value();
        const amount = Number(limit.value);
        if (!category) { message.textContent = "Choose a category."; return; }
        if (!(amount > 0)) { message.textContent = "Enter a limit above zero."; return; }
        try {
          await api(`/api/budgets/${encodeURIComponent(category)}`, { method: "PUT", body: { monthly_limit: amount } });
          closeSheet(); toast("Budget saved."); reload();
        } catch (err) { message.textContent = err instanceof SignedOut ? "Signed out." : err.message; }
      };
      openSheet(
        el("h2", { text: fixed ? categoryLabel(fixed) : "Add a budget" }),
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

    // Clicking a category (or sub-category) opens a popup listing the transactions behind its total for the month.
    async function showTransactions(category) {
      const label = categoryLabel(category);
      const title = `${label} in ${monthLabel(month)}`;
      const body = el("div", {}, el("p", { class: "empty", text: "Loading…" }));
      openSheet(el("h2", { text: title }), body, el("div", { class: "actions" }, el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })));
      try {
        const lastDay = new Date(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0).getDate();
        const q = new URLSearchParams({ category, start: `${month}-01`, end: `${month}-${String(lastDay).padStart(2, "0")}`, limit: "200" });
        if (viewer != null) q.set("person_id", String(viewer));
        const [page, accounts] = await Promise.all([api(`/api/transactions?${q}`), api(scoped("/api/accounts"))]);
        if (id !== navId || !body.isConnected) return;   // moved to another page, or another popup replaced this one
        const names = Object.fromEntries(accounts.map((a) => [a.id, `${a.institution ? a.institution + " " : ""}${a.name}`]));
        const net = page.transactions.reduce((sum, t) => sum + (t.currency === cur ? t.amount : 0), 0);
        const rows = [];
        let day = null;
        for (const tx of page.transactions) {
          if (tx.date !== day) { day = tx.date; rows.push(el("div", { class: "dayhead", text: dayLabel(tx.date) })); }
          const amount = signed(tx.amount, tx.currency);
          const open = () => editFromPopup(tx, names, category);
          rows.push(el("div", { class: "row click", role: "button", tabindex: 0, onclick: open,
            onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } } },
            el("div", { class: "icon", text: icon(tx.category), "aria-hidden": "true" }),
            el("div", { class: "grow" },
              el("div", {}, el("strong", { text: tx.display_name }), tx.pending && el("span", { class: "badge", text: "Pending" })),
              el("div", { class: "muted", text: `${categoryLabel(tx.subcategory || tx.category)} \u00B7 ${names[tx.account_id] || ""}` })),
            el("div", { class: `amount ${amount.cls}`, text: amount.text })));
        }
        body.replaceChildren(...[
          el("p", { class: "muted", text: page.total === 0 ? "No transactions." : (page.total > page.transactions.length ? `${page.total} transactions. The newest ${page.transactions.length} add up to ${money(net, cur)}.` : `${page.total} transaction${page.total === 1 ? "" : "s"}, ${money(net, cur)} in total.`) }),
          page.total > 0 && el("p", { class: "muted", text: "Tap a transaction to change its category." }),
          ...rows,
          el("p", { class: "muted" }, el("a", { href: "#/transactions", onclick: closeSheet, text: "Search all transactions" }))].filter(Boolean));   // replaceChildren would print a false as the word "false"
      } catch (err) {
        if (err instanceof SignedOut) { closeSheet(); showLogin(); } else if (body.isConnected) body.replaceChildren(el("p", { class: "error", text: err.message }));
      }
    }
    // A transaction opens the usual detail sheet (category picker, reset, rule offer). When that sheet closes the popup comes back:
    // as it was if nothing changed, or after the page has reloaded with the new totals if the category was changed.
    function editFromPopup(tx, names, category) {
      const sheet = $("sheet");
      let changed = false;
      const back = () => {
        sheet.removeEventListener("close", back);
        if ($("shell").hidden || id !== navId) return;   // signed out, or moved to another page
        if (changed) { budgetPopup = category; reload(); } else showTransactions(category);
      };
      sheet.addEventListener("close", back);
      openTransaction(tx, choices, names, () => { changed = true; });
    }
    const pickable = (category) => ({ onclick: () => showTransactions(category), onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); showTransactions(category); } } });

    // Budgets live on the rows of "Spending by category": a budgeted category's bar is measured against its limit
    // (so the end of the bar is the max), and once spending passes the limit the bar runs on in red past a red line.
    const budgetOf = new Map(budgets.budgets.map((b) => [b.category, b]));
    const budgeted = new Set(budgetOf.keys());
    const rows = spending.categories.map((c) => ({ ...c, subcategories: [...(c.subcategories || [])] }));
    const rowOf = (code) => rows.find((r) => r.category === code) || (rows.push({ category: code, spent: 0, subcategories: [] }), rows[rows.length - 1]);
    for (const b of budgets.budgets) {   // a budget on a category with no spending yet still gets its row
      if (!b.parent) rowOf(b.category);
      else {
        const parent = rowOf(b.parent);
        if (!parent.subcategories.some((s) => s.category === b.category)) parent.subcategories.push({ category: b.category, spent: b.spent });
      }
    }
    const budgetNote = (b) => {
      const note = el("div", { class: "muted budget-note" }, `of ${money(b.monthly_limit, cur)} \u00B7 `,
        b.remaining >= 0 ? `${money(b.remaining, cur)} left` : el("span", { class: "neg", text: `${money(-b.remaining, cur)} over` }));
      if (b.parent && budgeted.has(b.parent)) note.append(` \u00B7 also counted in ${categoryLabel(b.parent)}`);
      return note;
    };
    // Every row has the same-sized slot for the button, so the bars line up whether or not a category has a budget.
    // A category with a budget gets Edit; one without gets a small + and target button that asks for the monthly amount.
    const editButton = (b, category) => el("span", { class: "budget-slot" }, b
      ? el("button", { type: "button", class: "secondary budget-edit", title: `Edit the ${categoryLabel(b.category)} budget`, "aria-label": `Edit ${categoryLabel(b.category)} budget`,
        onclick: (e) => { e.stopPropagation(); budgetDialog(b); }, onkeydown: (e) => e.stopPropagation() }, iconSvg("i-target", 16))
      : category && el("button", { type: "button", class: "secondary budget-add", title: `Add a budget for ${categoryLabel(category)}`, "aria-label": `Add a budget for ${categoryLabel(category)}`,
        onclick: (e) => { e.stopPropagation(); budgetDialog(null, category); }, onkeydown: (e) => e.stopPropagation() },
        el("span", { "aria-hidden": "true", text: "+" }), iconSvg("i-target", 16)));

    const maxSpent = Math.max(...rows.map((c) => Math.abs(c.spent)), 1);
    // The title has the month's total on its right (the longer explanation is gone).
    const spendCard = el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { text: "Spending by category" }), el("div", { class: "head-total", text: `Total spent ${money(spending.total, cur)}` })));
    if (rows.length === 0) spendCard.append(el("p", { class: "empty", text: "Nothing spent this month." }));
    for (const [index, c] of rows.entries()) {
      // The sub-categories are a finer view of the same money, so they are indented and never added to the total.
      // They start folded away; the arrow on the category's row opens and closes them.
      const split = c.subcategories;
      const rest = c.spent - split.reduce((sum, s) => sum + s.spent, 0);
      const parts = split.map((s) => [subLabel(s.category), s.spent, s.category]);
      if (split.length > 0 && Math.abs(rest) >= 0.005) parts.push(["Not in a sub-category", rest]);
      const group = el("div", { class: "sub-group", hidden: true }, parts.map(([label, spent, code]) => {
        const sb = code && budgetOf.get(code);
        return el("div", { class: `row sub${code ? " click" : ""}`, ...(code ? { role: "button", tabindex: 0, ...pickable(code) } : {}) },
          el("div", { class: "grow" }, el("div", { class: "muted", text: label }),
            sb ? budgetBar(spent, sb.monthly_limit) : progress((Math.max(spent, 0) / Math.max(c.spent, 0.01)) * 100, true), sb && budgetNote(sb)),
          el("div", { class: "amount muted", text: money(spent, cur) }), editButton(sb, code));
      }));
      let twisty = el("span", { class: "twisty" });
      if (parts.length > 0) {
        twisty = el("button", { type: "button", class: "twisty", "aria-expanded": "false", "aria-label": `Show the sub-categories of ${categoryLabel(c.category)}`, text: "\u25B8",
          onclick: (e) => {
            e.stopPropagation();  // the row itself opens the transactions
            group.hidden = !group.hidden;
            twisty.setAttribute("aria-expanded", String(!group.hidden));
            twisty.textContent = group.hidden ? "\u25B8" : "\u25BE";
          }, onkeydown: (e) => e.stopPropagation() });
      }
      const b = budgetOf.get(c.category);
      // Every other top-level category (with its sub-categories) has a very slightly different shade, to tell them apart at a glance.
      spendCard.append(el("div", { class: `cat-block${index % 2 === 1 ? " alt" : ""}` },
        el("div", { class: "row click", role: "button", tabindex: 0, ...pickable(c.category) },
          el("div", { class: "icon", text: icon(c.category), "aria-hidden": "true" }),
          el("div", { class: "grow" }, el("div", { text: categoryLabel(c.category) }),
            b ? budgetBar(c.spent, b.monthly_limit) : progress((Math.max(c.spent, 0) / maxSpent) * 100, true), b && budgetNote(b)),
          el("div", { class: "amount", text: money(c.spent, cur) }), editButton(b, c.category), twisty), group));
    }
    // The month bar and Add budget live in the page's pinned bar (like Investments' period choice), not in the scrolling page.
    const bar = $("subtabs");
    bar.replaceChildren(nav);
    bar.hidden = false;
    measureBars();
    view().replaceChildren(viewerNote(true), spendCard);
    if (budgetPopup) { const again = budgetPopup; budgetPopup = null; showTransactions(again); }
  }

  // ---------------------------------------------------------------- Cash flow

  // Income up and expenses down for each day, week or month, and underneath, on one shared scale, the
  // running totals and the net worth of the accounts in view (never a second y-axis). Hovering a bar
  // shows its biggest items; clicking one lists all of them, where each can be re-categorized or
  // counted / left out by hand. Choices last while the page is open; nothing is written to the browser's
  // storage (only theme.js keeps appearance there). The range and grouping are in the address, so a
  // bookmark reopens the same view.
  const CF_RANGES = [["mtd", "Month to date"], ["6m", "6 months"], ["12m", "12 months"], ["ytd", "Year to date"], ["all", "All time"], ["custom", "Custom…"]];
  const CF_GROUPS = [["day", "Day"], ["week", "Week"], ["month", "Month"]];
  const CF_BAR_KEYS = [["income", "Income", "cf-inc", "rect"], ["expenses", "Expenses", "cf-exp", "rect"]];
  const CF_LINE_KEYS = [["cum_income", "Income so far", "cf-inc"], ["cum_expenses", "Expenses so far", "cf-exp"],
    ["cum_net", "Saved (income − expenses)", "cf-saved"], ["nw", "Net worth change, accounts shown", "cf-nw"]];
  const CF_DEFAULTS = { range: "mtd", group: null, start: null, end: null, week_start: "mon", accounts: null, exclude: [], pending: true, transfers: false, hidden: [] };
  const cf = { ...CF_DEFAULTS };
  let cfZoom = [];
  const coarsePointer = () => matchMedia("(pointer: coarse)").matches;

  function cfQuery(extra = {}) {
    const q = new URLSearchParams({ range: cf.range, week_start: cf.week_start, include_pending: String(cf.pending), include_transfers: String(cf.transfers) });
    if (cf.group) q.set("group", cf.group);
    if (cf.range === "custom") { q.set("start", cf.start); q.set("end", cf.end); }
    if (cf.accounts) q.set("accounts", cf.accounts.join(","));
    if (cf.exclude.length) q.set("exclude_categories", cf.exclude.join(","));
    if (viewer != null) q.set("person_id", String(viewer));
    for (const [k, v] of Object.entries(extra)) q.set(k, v);
    return q;
  }

  // Round tick marks covering lo..hi (always including zero).
  function niceTicks(lo, hi, count = 4) {
    lo = Math.min(lo, 0); hi = Math.max(hi, 0);
    if (hi - lo < 1) hi = lo + 1;
    const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw)), norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
    return { lo: start, hi: end, ticks };
  }

  // A bar from the baseline side `base` to the outer end `outer`, rounded only at the outer end.
  function cfBar(x, w, base, outer, rounded, cls) {
    const h = Math.abs(outer - base);
    if (h < 0.5) return null;
    if (!rounded) return svg("rect", { class: cls, x, y: Math.min(base, outer), width: w, height: h });
    const r = Math.min(4, w / 2, h);
    const up = outer < base;
    const d = up
      ? `M${x},${base} V${outer + r} Q${x},${outer} ${x + r},${outer} H${x + w - r} Q${x + w},${outer} ${x + w},${outer + r} V${base} Z`
      : `M${x},${base} V${outer - r} Q${x},${outer} ${x + r},${outer} H${x + w - r} Q${x + w},${outer} ${x + w},${outer - r} V${base} Z`;
    return svg("path", { class: cls, d });
  }

  // `mount` is the element the cash flow is drawn into (the Spending page's Cash flow tab, which puts its heading above it);
  // without one it fills the whole page.
  async function viewCashflow(id, params, mount = null) {
    // An address like #/spending?tab=cashflow&range=6m&group=week opens that view.
    if (params.get("range")) {
      cf.range = params.get("range"); cf.group = params.get("group");
      cf.start = params.get("start"); cf.end = params.get("end");
    }
    let data = null, listData = null, pinned = null, hover = null, listSide = "all", drag = null, suppressClick = false, seq = 0;
    const cur = () => (data ? data.currency : baseCurrency || "CAD");
    const m0 = (v) => money(v, cur(), { maximumFractionDigits: 0 });
    const m2 = (v) => money(v, cur());
    const sgn = (v, digits = 2) => (v > 0 ? "+" : v < 0 ? MINUS : "") + money(Math.abs(v), cur(), digits === 0 ? { maximumFractionDigits: 0 } : undefined);
    const compact = (v) => (v < 0 ? MINUS : "") + money(Math.abs(v), cur(), { notation: "compact", maximumFractionDigits: 1 });
    const hidden = () => new Set(cf.hidden);
    const dShort = (text) => parseDay(text).toLocaleDateString("en-CA", { month: "short", day: "numeric" });

    // ---- page skeleton
    const rangeSeg = el("div", { class: "seg wide compact", role: "group", "aria-label": "Look-back period" });
    const groupSeg = el("div", { class: "seg compact", role: "group", "aria-label": "Group by" });
    const filtersBtn = el("button", { type: "button", class: "secondary cf-filters", onclick: () => openFilters() });
    const cStart = el("input", { type: "date", "aria-label": "From" });
    const cEnd = el("input", { type: "date", "aria-label": "To" });
    const customRow = el("div", { class: "toolbar cf-custom", hidden: true }, el("label", {}, "From ", cStart), el("label", {}, "To ", cEnd),
      el("button", { type: "button", class: "primary", text: "Apply", onclick: () => {
        if (!cStart.value || !cEnd.value) { toast("Pick both dates.", true); return; }
        Object.assign(cf, { range: "custom", start: cStart.value, end: cEnd.value, group: null }); cfZoom = []; load();
      } }));
    const tiles = el("div", { class: "cf-tiles" });
    const legend1 = el("div", { class: "cf-legend", role: "group", "aria-label": "Bars and lines on the chart" });
    const title1 = el("h2", { text: "Income and expenses" });
    const plot1 = el("div", { class: "cf-plot" });
    const empty = el("p", { class: "cf-empty", hidden: true, text: "No transactions in this range." });
    const chartView = el("div", {}, el("div", { class: "cf-plotwrap" }, plot1, empty));
    const notes = el("p", { class: "muted cf-notes" });
    // One switch below the chart for all the running-total lines at once (the legend chips still switch each one).
    const linesBox = el("input", { type: "checkbox", role: "switch", "aria-label": "Show the running totals" });
    linesBox.addEventListener("change", () => {
      const h = hidden();
      for (const [k] of CF_LINE_KEYS) { if (linesBox.checked) h.delete(k); else h.add(k); }
      cf.hidden = [...h]; renderLegends(); draw();
    });
    const linesToggle = el("label", { class: "switch-row cf-lines-toggle" },
      el("span", { class: "switch" }, linesBox, el("span", { class: "track", "aria-hidden": "true" })), el("span", {}, "Show running totals"));
    const zoomBtn = el("button", { type: "button", class: "secondary", hidden: true, text: "Reset zoom",
      onclick: () => { Object.assign(cf, cfZoom.pop()); cfZoom = []; load(); } });
    // The range, grouping and filters shape this chart (and the figures above it), so they sit at the top of its box.
    const controls = el("div", { class: "toolbar cf-controls" }, rangeSeg, groupSeg, filtersBtn);
    const chartCard = el("section", { class: "card cf-card" },
      controls, customRow,
      el("div", { class: "card-head" }, el("div", {}, title1, legend1), el("div", { class: "actions" }, zoomBtn)),
      chartView, linesToggle, notes);
    const listTitle = el("h2", { text: "" });
    const listSummary = el("p", { class: "muted" });
    const sideSeg = el("div", { class: "seg compact", role: "group", "aria-label": "Show" });
    const search = el("input", { type: "search", placeholder: "Search", "aria-label": "Search these transactions", maxlength: 100 });
    const sort = el("select", { "aria-label": "Sort" }, el("option", { value: "date", text: "Newest first" }), el("option", { value: "amount", text: "Largest first" }));
    const listRows = el("div");
    const leftOut = el("details", { class: "cf-left" });
    const prevBtn = el("button", { type: "button", class: "secondary", text: "‹", "aria-label": "Previous period", onclick: () => pinned && pinned.i > 0 && pin(pinned.i - 1, listSide) });
    const nextBtn = el("button", { type: "button", class: "secondary", text: "›", "aria-label": "Next period", onclick: () => pinned && pinned.i < data.buckets.length - 1 && pin(pinned.i + 1, listSide) });
    const listCard = el("section", { class: "card cf-list", hidden: true },
      el("div", { class: "card-head" }, listTitle, el("div", { class: "actions" }, prevBtn, nextBtn,
        el("button", { type: "button", class: "secondary", text: "Close", onclick: () => unpin() }))),
      listSummary, el("div", { class: "toolbar" }, sideSeg, search, sort), listRows, leftOut);
    const tip = el("div", { class: "cf-tip", role: "tooltip", hidden: true });
    document.body.append(tip);
    cleanups.push(() => tip.remove());

    const parts = [tiles, chartCard, listCard];
    if (mount) mount.replaceChildren(el("h2", { class: "cf-page-heading", text: "Cash flow" }), ...parts); else view().replaceChildren(viewerNote(), ...parts);

    // ---- controls
    function allowedGroups() {
      const days = (a, b) => (parseDay(b) - parseDay(a)) / 864e5 + 1;
      const span = cf.range === "custom" && cf.start && cf.end ? days(cf.start, cf.end)
        : cf.range === "mtd" ? 31 : cf.range === "6m" ? 184 : cf.range === "all" ? (data && data.range.key === "all" ? days(data.range.start, data.range.end) : 731) : 366;
      return { day: span <= 366, week: span > 7 || cf.range === "mtd", month: cf.range !== "mtd" && span >= 28 };
    }
    function paintControls() {
      rangeSeg.replaceChildren(...CF_RANGES.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(cf.range === key), text: label,
        onclick: () => {
          cfZoom = [];
          if (key === "custom") {
            cf.range = "custom"; cf.group = null;
            cStart.value = cf.start || (data ? data.range.start : ""); cEnd.value = cf.end || (data ? data.range.end : "");
            paintControls(); cStart.focus(); return;
          }
          Object.assign(cf, { range: key, group: null }); load();
        } })));
      customRow.hidden = cf.range !== "custom";
      const allowed = allowedGroups(), shown = data ? data.group : cf.group;
      groupSeg.replaceChildren(...CF_GROUPS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(shown === key), text: label,
        disabled: !allowed[key], title: allowed[key] ? null : key === "day" ? "Grouping by day is limited to one year" : "This range is shorter than a month",
        onclick: () => { cf.group = key; load(); } })));
      const n = (cf.accounts ? 1 : 0) + cf.exclude.length + (cf.pending ? 0 : 1) + (cf.transfers ? 1 : 0) + (cf.week_start !== "mon" ? 1 : 0);
      filtersBtn.textContent = n ? `Filters (${n})` : "Filters";
      zoomBtn.hidden = cfZoom.length === 0;
    }

    function rememberAddress() {
      const q = new URLSearchParams({ range: cf.range });
      if (cf.group) q.set("group", cf.group);
      if (cf.range === "custom" && cf.start && cf.end) { q.set("start", cf.start); q.set("end", cf.end); }
      history.replaceState(null, "", `#/spending?tab=cashflow&${q}`);  // no hashchange, so no reload
    }

    // ---- data
    async function load(keepPin = false) {
      rememberAddress(); paintControls();
      const mine = ++seq;
      chartCard.classList.add("cf-loading");  // keep the last chart, dimmed, while the new one loads
      try {
        const next = await api(`/api/cashflow?${cfQuery()}`);
        if (mine !== seq || id !== navId) return;
        data = next;
        const keep = keepPin && pinned ? data.buckets.findIndex((b) => b.start === pinned.start) : -1;
        if (keep < 0) unpin(); else pinned = { i: keep, start: data.buckets[keep].start };
        render();
        if (keep >= 0) await loadList();
      } catch (err) {
        if (err instanceof SignedOut) { showLogin(); return; }
        if (mine !== seq) return;
        if (err.status === 400 && cf.group) { cf.group = null; return load(keepPin); }  // that grouping no longer fits
        toast(err.message, true);
      } finally { if (mine === seq) chartCard.classList.remove("cf-loading"); }
    }

    // ---- tiles
    function tile(label, value, sub, extra) {
      return el("div", { class: "cf-tile" }, el("div", { class: "muted", text: label }), el("strong", { text: value }), sub, extra);
    }
    function delta(curValue, prevValue, upIsGood) {
      const p = data.previous;
      if (!p) return el("div", { class: "muted cf-small", text: "All of your history" });
      if (!prevValue) return el("div", { class: "muted cf-small", text: "Nothing to compare with" });
      const change = (curValue - prevValue) / Math.abs(prevValue), up = change >= 0;
      const tone = Math.abs(change) < 0.005 ? "" : up === upIsGood ? "income" : "cf-bad";
      return el("div", { class: "muted cf-small", title: `${m0(prevValue)} from ${dayLabel(p.start)} to ${dayLabel(p.end)}` },
        el("span", { class: tone, text: `${up ? "▲" : "▼"} ${Math.abs(change * 100).toFixed(Math.abs(change) < 0.1 ? 1 : 0)}%` }),
        ` vs ${shortDate(p.start)} – ${shortDate(p.end)}`);
    }
    function renderTiles() {
      const t = data.totals, p = data.previous || {};
      tiles.replaceChildren(
        tile("Income", m0(t.income), delta(t.income, p.income, true)),
        tile("Expenses", m0(t.expenses), delta(t.expenses, p.expenses, false)),
        tile("Net", sgn(t.net, 0), el("div", { class: "muted cf-small", text: `Average ${sgn(t.avg_income - t.avg_expenses, 0)} per ${data.group}` })),
        tile("Savings rate", t.savings_rate == null ? "—" : `${Math.round(t.savings_rate * 100)}%`, el("div", { class: "muted cf-small", text: "Net ÷ income" })),
        tile("Net worth change", sgn(t.net_worth_change, 0), el("div", { class: "muted cf-small", text: `${m0(t.net_worth_start)} → ${m0(t.net_worth_end)}` })));
    }

    // ---- legends (they double as switches)
    function legendChip(key, label, cls, shape) {
      const on = !hidden().has(key);
      const swatch = shape === "rect" ? svg("rect", { x: 3, y: 0, width: 10, height: 10, rx: 2, class: `fill ${cls}` })
        : shape === "dot" ? svg("circle", { cx: 8, cy: 5, r: 4, class: `fill ${cls}` })
        : svg("line", { x1: 0, x2: 16, y1: 5, y2: 5, class: `stroke ${cls}` });
      return el("button", { type: "button", class: "cf-chip", "aria-pressed": String(on), title: `Show or hide ${label.toLowerCase()}`,
        onclick: () => { const h = hidden(); if (h.has(key)) h.delete(key); else h.add(key); cf.hidden = [...h]; renderLegends(); draw(); } },
      svg("svg", { width: 16, height: 10, "aria-hidden": "true" }, swatch), label);
    }
    function renderLegends() {
      linesBox.checked = CF_LINE_KEYS.some(([k]) => !hidden().has(k));
      legend1.replaceChildren(...CF_BAR_KEYS.map(([k, l, c, s]) => legendChip(k, l, c, s)), ...CF_LINE_KEYS.map(([k, l, c]) => legendChip(k, l, c, "line")));
    }

    // ---- charts
    let geo = null;  // the last drawing's geometry, for pointer maths
    function draw() {
      if (!data) return;
      const width = Math.max(300, Math.round(plot1.clientWidth || 640));
      const narrow = width < 560;
      const L = narrow ? 58 : 60, R = 10, T = 10, B = narrow ? 28 : 24;
      const H1 = narrow ? 300 : 380;
      const bks = data.buckets, n = bks.length, slot = (width - L - R) / n;
      const cx = (i) => L + slot * (i + 0.5);
      const off = hidden();
      const barW = Math.max(2, Math.min(24, slot * 0.62));

      // one chart, one scale: the period bars (income up, expenses down) and the running-total lines
      const lineVals = {
        cum_income: bks.map((b) => b.cum_income), cum_expenses: bks.map((b) => -b.cum_expenses),
        cum_net: bks.map((b) => b.cum_net), nw: bks.map((b) => b.net_worth - data.totals.net_worth_start),
      };
      const visible = CF_LINE_KEYS.filter(([k]) => !off.has(k));
      const shown = [0, ...bks.map((b) => (off.has("income") ? 0 : b.income)), ...bks.map((b) => (off.has("expenses") ? 0 : -b.expenses)),
        ...visible.flatMap(([k]) => lineVals[k])];
      const top = niceTicks(Math.min(...shown), Math.max(...shown), narrow ? 5 : 6);
      top.lo = Math.min(top.lo, ...shown);
      top.hi = Math.max(top.hi, ...shown);
      const y1 = (v) => T + (1 - (v - top.lo) / (top.hi - top.lo || 1)) * (H1 - T - B);
      const s1 = svg("svg", { class: "chart cf-chart", width, height: H1, viewBox: `0 0 ${width} ${H1}`, tabindex: 0, role: "img",
        "aria-label": `Income ${m0(data.totals.income)}, expenses ${m0(data.totals.expenses)}, saved ${sgn(data.totals.net, 0)} and net worth change ${sgn(data.totals.net_worth_change, 0)} from ${dayLabel(data.range.start)} to ${dayLabel(data.range.end)}. Use the arrow keys to move between periods and Enter to list one.` });
      const band1 = svg("rect", { class: "cf-band", y: T, height: H1 - T - B, width: 0 });
      const pinBand1 = svg("rect", { class: "cf-pin", y: T, height: H1 - T - B, width: 0 });
      s1.append(pinBand1, band1);
      for (const v of top.ticks) {
        s1.append(svg("line", { class: v === 0 ? "gl cf-zero" : "gl", x1: L, x2: width - R, y1: y1(v), y2: y1(v) }));
        const label = svg("text", { x: L - 6, y: y1(v) + 4, "text-anchor": "end" });
        label.textContent = compact(v);
        s1.append(label);
      }
      const zero = y1(0);
      bks.forEach((b, i) => {
        const x = cx(i) - barW / 2, part = b.partial ? " cf-partial" : "";
        if (!off.has("income")) {
          const posted = b.income - b.pending_income;
          s1.append(cfBar(x, barW, zero, y1(posted), !b.pending_income, `fill cf-inc${part}`));
          if (b.pending_income) s1.append(cfBar(x, barW, y1(posted), y1(b.income), true, `fill cf-inc cf-pending${part}`));
        }
        if (!off.has("expenses")) {
          const posted = b.expenses - b.pending_expenses;
          s1.append(cfBar(x, barW, zero, y1(-posted), !b.pending_expenses, `fill cf-exp${part}`));
          if (b.pending_expenses) s1.append(cfBar(x, barW, y1(-posted), y1(-b.expenses), true, `fill cf-exp cf-pending${part}`));
        }
      });
      for (const [key, , cls] of visible) {
        const pts = lineVals[key];
        if (n === 1) { s1.append(svg("circle", { class: `cf-end ${cls}`, cx: cx(0), cy: y1(pts[0]), r: 4 })); continue; }
        s1.append(svg("path", { class: `cf-line ${cls}`, d: pts.map((v, i) => `${i ? "L" : "M"}${cx(i).toFixed(1)},${y1(v).toFixed(1)}`).join(" ") }));
        s1.append(svg("circle", { class: `cf-end ${cls}`, cx: cx(n - 1), cy: y1(pts[n - 1]), r: 4 }));
      }
      const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor((width - L - R) / (narrow ? 60 : 66)))));
      bks.forEach((b, i) => {
        if (i % every) return;
        const label = svg("text", { x: cx(i), y: H1 - 6, "text-anchor": "middle" });
        label.textContent = b.short_label;
        s1.append(label);
      });
      const drag1 = svg("rect", { class: "cf-drag", y: T, height: H1 - T - B, width: 0 });
      s1.append(drag1);

      plot1.replaceChildren(s1);
      empty.hidden = bks.some((b) => b.income_count || b.expense_count);
      geo = { L, R, T, width, slot, cx, zero, s1, band1, pinBand1, drag1, n };
      wire(s1, true);
      paintBands();
    }

    function paintBands() {
      if (!geo) return;
      const place = (rect, i, j = i) => {
        if (i == null) { rect.setAttribute("width", 0); return; }
        const a = Math.min(i, j), b = Math.max(i, j);
        rect.setAttribute("x", geo.L + geo.slot * a); rect.setAttribute("width", geo.slot * (b - a + 1));
      };
      place(geo.band1, hover);
      place(geo.pinBand1, pinned ? pinned.i : null);
      place(geo.drag1, drag && drag.moved ? drag.i : null, drag ? drag.j : null);
    }

    function indexAt(svgEl, clientX) {
      const box = svgEl.getBoundingClientRect(), px = clientX - box.left;
      if (px < geo.L - 4 || px > geo.width - geo.R + 4) return null;
      return Math.max(0, Math.min(geo.n - 1, Math.floor((px - geo.L) / geo.slot)));
    }
    const sideAt = (clientY) => (clientY - geo.s1.getBoundingClientRect().top < geo.zero ? "income" : "expense");

    function wire(svgEl, isBars) {
      svgEl.addEventListener("pointermove", (e) => {
        if (e.pointerType === "touch") return;
        const i = indexAt(svgEl, e.clientX);
        if (drag && isBars) {
          if (i != null && i !== drag.j) { drag.j = i; drag.moved = drag.moved || i !== drag.i; paintBands(); }
          return;
        }
        hover = i; paintBands();
        if (i == null) { tip.hidden = true; return; }
        showTip(i, isBars ? sideAt(e.clientY) : null, svgEl, e.clientY);
      });
      svgEl.addEventListener("pointerleave", () => { if (!drag) { hover = null; paintBands(); tip.hidden = true; } });
      svgEl.addEventListener("click", (e) => {
        if (suppressClick) { suppressClick = false; return; }
        const i = indexAt(svgEl, e.clientX);
        if (i != null) pin(i, isBars && !coarsePointer() ? sideAt(e.clientY) : "all");
      });
      if (!isBars) return;
      svgEl.addEventListener("pointerdown", (e) => {
        if (e.pointerType !== "mouse" || e.button !== 0) return;
        const i = indexAt(svgEl, e.clientX);
        if (i != null) drag = { i, j: i, moved: false };
      });
      svgEl.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          const from = hover ?? (pinned ? pinned.i : e.key === "ArrowRight" ? -1 : geo.n);
          hover = Math.max(0, Math.min(geo.n - 1, from + (e.key === "ArrowRight" ? 1 : -1)));
          paintBands();
          const box = svgEl.getBoundingClientRect();
          showTip(hover, "expense", svgEl, box.top + geo.zero + 20);
        } else if ((e.key === "Enter" || e.key === " ") && hover != null) {
          e.preventDefault(); pin(hover, "all");
        } else if (e.key === "Escape") { tip.hidden = true; unpin(); }
      });
      svgEl.addEventListener("blur", () => { hover = null; paintBands(); tip.hidden = true; });
    }
    function endDrag() {
      if (!drag) return;
      const d = drag; drag = null;
      if (d.moved && d.i !== d.j && data) {
        suppressClick = true; setTimeout(() => { suppressClick = false; }, 0);
        const a = data.buckets[Math.min(d.i, d.j)], b = data.buckets[Math.max(d.i, d.j)];
        cfZoom.push({ range: cf.range, group: cf.group, start: cf.start, end: cf.end });
        Object.assign(cf, { range: "custom", start: a.start, end: b.end, group: null });
        tip.hidden = true; load();
      } else paintBands();
    }
    window.addEventListener("pointerup", endDrag);
    cleanups.push(() => window.removeEventListener("pointerup", endDrag));

    // ---- hover box
    function tipRow(value, cls, shape, label) {
      const sw = shape === "line" ? svg("line", { x1: 0, x2: 14, y1: 5, y2: 5, class: `stroke ${cls}` })
        : shape === "dot" ? svg("circle", { cx: 7, cy: 5, r: 4, class: `fill ${cls}` })
        : svg("rect", { x: 2, y: 0, width: 10, height: 10, rx: 2, class: `fill ${cls}` });
      return el("div", { class: "cf-tiprow" }, el("strong", { text: value }), svg("svg", { width: 14, height: 10, "aria-hidden": "true" }, sw), el("span", { class: "muted", text: label }));
    }
    function showTip(i, side, svgEl, clientY) {
      if (coarsePointer()) return;
      const b = data.buckets[i];
      const parts = [el("div", { class: "cf-tiphead", text: b.label + (b.partial ? " (partial)" : "") })];
      {
        parts.push(tipRow(sgn(b.income), "cf-inc", "rect", `Income · ${b.income_count}`), tipRow(sgn(-b.expenses), "cf-exp", "rect", `Expenses · ${b.expense_count}`));
        if (b.pending_income || b.pending_expenses) parts.push(el("div", { class: "muted cf-small", text: `Includes ${m2(b.pending_income + b.pending_expenses)} pending` }));
        const hiddenNow = hidden();
        const lineRows = [["cum_income", sgn(b.cum_income, 0), "Income so far"], ["cum_expenses", sgn(-b.cum_expenses, 0), "Expenses so far"],
          ["cum_net", sgn(b.cum_net, 0), "Saved"], ["nw", sgn(b.net_worth - data.totals.net_worth_start, 0), "Net worth change"]]
          .filter(([k]) => !hiddenNow.has(k));
        if (lineRows.length) parts.push(el("div", { class: "cf-tipsect" }, lineRows.map(([k, v, l]) => tipRow(v, CF_LINE_KEYS.find((x) => x[0] === k)[2], "line", l))));
        if (side === "expense" && b.top_categories.length) {
          const cats = b.top_categories.slice(0, 3), other = b.expenses - cats.reduce((s, c) => s + c.amount, 0);
          parts.push(el("div", { class: "cf-tipsect" }, el("div", { class: "muted", text: "By category" }),
            cats.map((c) => el("div", { class: "cf-tipline" }, el("span", { text: categoryLabel(c.category) }), el("span", { class: "amount", text: m0(c.amount) }))),
            Math.abs(other) >= 0.5 && el("div", { class: "cf-tipline muted" }, el("span", { text: "Everything else" }), el("span", { class: "amount", text: m0(other) }))));
        }
        const list = side === "income" ? b.top_income : b.top_expenses, count = side === "income" ? b.income_count : b.expense_count;
        if (list.length) {
          parts.push(el("div", { class: "cf-tipsect" }, el("div", { class: "muted", text: side === "income" ? "Largest income" : "Largest expenses" }),
            list.map((t) => el("div", { class: "cf-tiptx" }, el("span", { class: "muted", text: dShort(t.date) }),
              el("span", { class: "cf-ell", text: t.display_name + (t.pending ? " · pending" : "") }),
              el("span", { class: "amount", text: sgn(side === "income" ? t.value : -t.value) }))),
            el("div", { class: "muted cf-small", text: count > list.length ? `+ ${count - list.length} more · click to see all` : "Click to see and edit them" })));
        }
      }
      tip.replaceChildren(...parts);
      tip.hidden = false;
      const box = svgEl.getBoundingClientRect(), x = box.left + geo.cx(i), w = tip.offsetWidth, h = tip.offsetHeight, gap = 16;
      let left = x + gap;
      if (left + w > innerWidth - 8) left = x - gap - w;
      tip.style.left = `${Math.max(8, left)}px`;
      tip.style.top = `${Math.max(8, Math.min(innerHeight - h - 8, clientY - h / 2))}px`;
    }

    // ---- the transactions behind one bar
    async function pin(i, side) {
      if (!data || !data.buckets[i]) return;
      pinned = { i, start: data.buckets[i].start };
      listSide = side || "all";
      tip.hidden = true; paintBands();
      await loadList();
      if (id === navId) listCard.scrollIntoView({ behavior: "smooth", block: coarsePointer() ? "start" : "nearest" });
    }
    function unpin() { pinned = null; listCard.hidden = true; paintBands(); }
    async function loadList() {
      const b = data.buckets[pinned.i];
      listTitle.textContent = b.label + (b.partial ? " (partial)" : "");
      listSummary.textContent = "Loading…";
      listCard.hidden = false;
      try {
        listData = await api(`/api/cashflow/transactions?${cfQuery({ period_start: b.start, period_end: b.end })}`);
        if (id === navId) renderList();
      } catch (err) { if (err instanceof SignedOut) showLogin(); else listSummary.textContent = err.message; }
    }
    function cfTxRow(t) {
      const shown = t.side === "income" ? t.value : -t.value;
      const why = t.reason ? ` · ${t.reason}` : "";
      const row = el("div", { class: "row click", role: "button", tabindex: 0 },
        el("div", { class: "icon", text: icon(t.category), "aria-hidden": "true" }),
        el("div", { class: "grow" },
          el("div", {}, el("strong", { text: t.display_name }), t.pending && el("span", { class: "badge", text: "Pending" }),
            t.cashflow_override && el("span", { class: "badge", text: t.cashflow_override === "include" ? "Counted by you" : "Left out by you" })),
          el("div", { class: "muted", text: `${dShort(t.date)} · ${categoryLabel(t.category)} · ${t.account_name}${why}` })),
        el("div", { class: `amount ${shown > 0 ? "income" : ""}`, text: sgn(shown) }));
      const open = () => openCashflowTx(t);
      row.addEventListener("click", open);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      return row;
    }
    function renderList() {
      sideSeg.replaceChildren(...[["all", "All"], ["income", "Income"], ["expense", "Expenses"]].map(([key, label]) =>
        el("button", { type: "button", "aria-pressed": String(listSide === key), text: label, onclick: () => { listSide = key; renderList(); } })));
      listSummary.textContent = `Income ${sgn(listData.income)} · Expenses ${sgn(-listData.expenses)} · Net ${sgn(listData.net)}`;
      const term = search.value.trim().toLowerCase();
      const keep = (t) => (listSide === "all" || t.side === listSide)
        && (!term || `${t.display_name} ${t.name} ${t.account_name} ${categoryLabel(t.category)}`.toLowerCase().includes(term));
      const order = sort.value === "amount" ? (a, b) => Math.abs(b.value) - Math.abs(a.value) : (a, b) => b.date.localeCompare(a.date);
      const rows = listData.transactions.filter(keep).sort(order);
      listRows.replaceChildren(...(rows.length ? rows.map(cfTxRow) : [el("p", { class: "empty", text: "Nothing here." })]));
      const left = listData.left_out.filter(keep);
      leftOut.hidden = left.length === 0;
      leftOut.replaceChildren(el("summary", { text: `Left out of cash flow (${left.length})` }), ...left.map(cfTxRow));
      prevBtn.disabled = pinned.i === 0; nextBtn.disabled = pinned.i === data.buckets.length - 1;
    }
    search.addEventListener("input", () => listData && renderList());
    sort.addEventListener("change", () => listData && renderList());

    async function openCashflowTx(t) {
      const choices = await categoryChoices([t.category]);
      const picker = categoryPicker(choices, t.category);
      const shown = t.side === "income" ? t.value : -t.value;
      const status = t.counted ? `Counted as ${t.side === "income" ? "income" : "an expense"}.` : `Left out: ${t.reason}.`;
      const done = async (body, message) => {
        try {
          await api(`/api/transactions/${encodeURIComponent(t.id)}`, { method: "PATCH", body });
          closeSheet(); toast(message); await load(true);
        } catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
      };
      openSheet(
        el("h2", { text: t.display_name }),
        el("p", { class: "muted", text: `${dayLabel(t.date)} · ${t.account_name}` }),
        el("div", { class: `big small ${shown > 0 ? "income" : ""}`, text: sgn(shown) }),
        el("p", { text: status }),
        el("div", { class: "actions" },
          t.counted
            ? el("button", { type: "button", class: "secondary", text: "Leave out of cash flow", onclick: () => done({ cashflow: "exclude" }, "Left out of cash flow") })
            : el("button", { type: "button", class: "secondary", text: "Count in cash flow", onclick: () => done({ cashflow: "include" }, "Counted in cash flow") }),
          t.cashflow_override && el("button", { type: "button", class: "secondary", text: "Back to automatic", onclick: () => done({ cashflow: null }, "Back to automatic") })),
        el("h3", { text: "Category" }), picker.node,
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Save category", onclick: () => { const v = picker.value(); if (v) done({ category: v }, "Category changed"); } }),
          t.category_source === "manual" && el("button", { type: "button", class: "secondary", text: "Reset to automatic", onclick: () => done({ category: null }, "Category reset") }),
          el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })));
    }

    // ---- filters
    async function openFilters() {
      let accounts, choices;
      try { [accounts, choices] = await Promise.all([api(scoped("/api/accounts")), categoryChoices()]); }
      catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); return; }
      const defaults = accounts.filter((a) => !a.hidden && a.type !== "investment").map((a) => a.id);
      const chosen = new Set(cf.accounts || defaults);
      const acctBoxes = accounts.map((a) => {
        const box = el("input", { type: "checkbox", value: a.id, checked: chosen.has(a.id) });
        return el("label", { class: "row" }, box, el("span", { class: "grow", text: `${a.institution ? a.institution + " · " : ""}${a.name}${a.currency !== cur() ? ` (${a.currency})` : ""}${a.type === "investment" ? " · investments" : ""}` }));
      });
      const tops = choices.filter((c) => !parentOf(c));
      const catBoxes = tops.map((c) => el("label", { class: "row" }, el("input", { type: "checkbox", value: c, checked: cf.exclude.includes(c) }), el("span", { class: "grow", text: categoryLabel(c) })));
      const pending = el("input", { type: "checkbox", checked: cf.pending });
      const transfers = el("input", { type: "checkbox", checked: cf.transfers });
      const week = el("select", { "aria-label": "Weeks start on" }, el("option", { value: "mon", text: "Monday", selected: cf.week_start === "mon" }), el("option", { value: "sun", text: "Sunday", selected: cf.week_start === "sun" }));
      openSheet(
        el("h2", { text: "Cash flow filters" }),
        el("h3", { text: "Accounts" }), el("p", { class: "muted", text: "Investment accounts are off by default: buying and selling isn't income or spending, and market moves would swamp the net-worth line." }),
        el("div", { class: "cf-checks" }, acctBoxes),
        el("h3", { text: "Leave out categories" }), el("div", { class: "cf-checks" }, catBoxes),
        el("h3", { text: "Options" }),
        el("label", { class: "row" }, pending, el("span", { class: "grow", text: "Include pending transactions" })),
        el("label", { class: "row" }, transfers, el("span", { class: "grow", text: "Count transfers between my accounts and investment contributions" })),
        el("label", { class: "row" }, el("span", { class: "grow", text: "Weeks start on" }), week),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary", text: "Apply", onclick: () => {
            const ids = acctBoxes.map((l) => l.querySelector("input")).filter((b) => b.checked).map((b) => b.value);
            const same = ids.length === defaults.length && defaults.every((d) => ids.includes(d));
            Object.assign(cf, { accounts: same ? null : ids, exclude: catBoxes.map((l) => l.querySelector("input")).filter((b) => b.checked).map((b) => b.value),
              pending: pending.checked, transfers: transfers.checked, week_start: week.value });
            closeSheet(); load();
          } }),
          el("button", { type: "button", class: "secondary", text: "Reset", onclick: () => {
            for (const l of acctBoxes) { const b = l.querySelector("input"); b.checked = defaults.includes(b.value); }
            for (const l of catBoxes) l.querySelector("input").checked = false;
            pending.checked = true; transfers.checked = false; week.value = "mon";
          } }),
          el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
    }

    function render() {
      title1.textContent = `Income and expenses per ${data.group}`;
      paintControls(); renderTiles(); renderLegends(); draw();
      notes.textContent = data.notes.join(" ");
    }

    // Redraw at the new width when the window or sidebar changes size, so text stays crisp.
    let frame = 0;
    const resize = new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { if (geo && Math.abs((plot1.clientWidth || 0) - geo.width) > 2) draw(); }); });
    resize.observe(plot1);
    cleanups.push(() => resize.disconnect());

    paintControls();
    await load();
  }

  // ---------------------------------------------------------------- Investments

  const CHANGE_PERIODS = [["day", "Day"], ["open", "Since open"], ["1w", "1W"], ["1m", "1M"], ["3m", "3M"], ["ytd", "YTD"], ["1y", "1Y"]];
  // Phrases that finish "since …" / "over …" after the figures (upperFirst turns one into the line's label), not whole sentences.
  const PERIOD_HELP = {
    day: "since yesterday's close", open: "since today's open", "1w": "over the last 7 days",
    "1m": "over the last month", "3m": "over the last 3 months", ytd: "since the start of the year", "1y": "over the last year",
  };
  let invPeriod = "day";
  let syncPolls = 0;  // first-sync checks on the Settings screen since you last navigated or linked

  const upperFirst = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  // On the Investments page the currency is named next to each total, so amounts there use a plain "$" (not "US$").
  // In the Canadian locale "$" means CAD, so US dollars are formatted in the US locale to get a plain "$".
  const shortFormatters = {};
  const moneyShort = (value, currency) => {
    shortFormatters[currency] ??= new Intl.NumberFormat(currency === "USD" ? "en-US" : "en-CA", { style: "currency", currency, currencyDisplay: "narrowSymbol" });
    return shortFormatters[currency].format(value);
  };
  function changeText(change, currency) {
    if (!change) return null;
    const sign = change.amount > 0 ? "+" : change.amount < 0 ? MINUS : "";
    const percent = change.percent == null ? "" : ` (${sign}${Math.abs(change.percent).toFixed(2)}%)`;
    return { text: `${sign}${moneyShort(Math.abs(change.amount), currency)}${percent}`, cls: change.amount > 0 ? "income" : change.amount < 0 ? "neg" : "muted" };
  }

  // ---- Investment activity: buys, sells, dividends and fees with their dates ---------------------
  const INV_LABELS = { buy: "Bought", sell: "Sold", fee: "Fee", transfer: "Transfer", cancel: "Cancelled" };
  const invLabel = (t) => INV_LABELS[t.type] || (t.subtype && t.subtype !== "other" ? titleCase(t.subtype) : titleCase(t.type));
  function investmentRow(t, showAccount) {
    const amount = signed(t.amount, t.currency);
    const detail = t.quantity && t.price ? `${Math.abs(t.quantity)} @ ${money(t.price, t.currency)}${t.fees ? ` \u00B7 fee ${money(t.fees, t.currency)}` : ""}` : null;
    return el("div", { class: "row" },
      el("div", { class: "grow" },
        el("div", {}, el("strong", { text: invLabel(t) }), t.ticker && el("span", { class: "muted", text: `  ${t.ticker}` })),
        el("div", { class: "muted", text: [shortDate(t.date), showAccount && t.account_name, detail].filter(Boolean).join(" \u00B7 ") })),
      el("div", { class: `amount ${amount.cls}`, text: amount.text }));
  }
  // A list that loads 50 at a time with a "Show more" button. `query` is the extra URL parameters.
  function investmentActivity(query, showAccount, id) {
    const list = el("div");
    const status = el("p", { class: "empty" });
    const more = el("button", { type: "button", text: "Show more", hidden: true });
    let offset = 0;
    async function load(reset) {
      if (reset) { offset = 0; list.replaceChildren(); }
      status.textContent = "Loading\u2026"; more.hidden = true;
      try {
        const q = new URLSearchParams({ limit: "50", offset: String(offset), ...query() });
        if (viewer != null) q.set("person_id", String(viewer));
        const page = await api(`/api/investments/transactions?${q}`);
        if (id !== navId) return;
        offset += page.transactions.length;
        list.append(...page.transactions.map((t) => investmentRow(t, showAccount)));
        status.textContent = page.total === 0 ? "No activity yet. Your bank may not share it, or it arrives with the next sync." : "";
        more.hidden = offset >= page.total;
      } catch (err) { if (err instanceof SignedOut) showLogin(); else status.textContent = err.message; }
    }
    more.addEventListener("click", () => load(false));
    return { node: el("div", {}, list, status, more), load };
  }

  let invTab = "holdings";
  const INV_TABS = [["holdings", "Holdings"], ["performance", "Performance"]];
  // The Investments tabs sit at the top of the pinned bar; each tab adds its own controls underneath.
  const invTabs = () => el("div", { class: "seg", role: "group", "aria-label": "Investments sections" },
    INV_TABS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === invTab), text: label,
      onclick: () => { location.hash = `#/investments${key === "holdings" ? "" : `?tab=${key}`}`; } })));
  // Inside Performance: the results, or playing out what would have happened if you had not sold.
  let perfSection = "results";
  const PERF_SECTIONS = [["results", "Results"], ["whatifs", "What ifs"]];
  const perfSections = () => el("div", { class: "seg perf-sections", role: "group", "aria-label": "Performance sections" },
    PERF_SECTIONS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === perfSection), text: label,
      onclick: () => { if (key !== perfSection) { perfSection = key; guard(viewInvestments(++navId)); } } })));
  const viewInvestments = (id) => (invTab === "performance" ? (perfSection === "whatifs" ? viewWhatIfs(id) : viewPerformance(id)) : viewHoldings(id));

  async function viewHoldings(id) {
    $("page-sub").textContent = PAGES.investments[1];
    let timer = null;
    let lastData = null;      // the latest figures, so choosing a period repaints without another request
    // The period choice is a pinned bar just under the page choices (the page's own bar, like Spending's tabs), not part of the scrolling page.
    const seg = el("div", { class: "seg period-seg", role: "group", "aria-label": "Compare against" },
      CHANGE_PERIODS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === invPeriod), text: label,
        onclick: () => { invPeriod = key; syncSeg(); if (lastData) paint(lastData); } })));
    const syncSeg = () => seg.querySelectorAll("button").forEach((btn, i) => btn.setAttribute("aria-pressed", String(CHANGE_PERIODS[i][0] === invPeriod)));
    const bar = $("subtabs");
    bar.replaceChildren(invTabs(), seg);
    bar.hidden = false;
    measureBars();
    let banks = new Map();    // account id -> its bank's logo item and name, loaded once
    const loadBanks = async () => {
      try {
        const [accounts, items] = await Promise.all([api(scoped("/api/accounts")), api("/api/items")]);
        const byItem = new Map(items.map((i) => [i.id, i]));
        banks = new Map(accounts.map((a) => [a.id, { item: byItem.get(a.item_id), name: a.institution || (byItem.get(a.item_id) || {}).institution }]));
      } catch (err) { if (err instanceof SignedOut) throw err; }   // without them the initials badge is used
    };
    const bookOf = (list, cur) => {
      const mine = list.filter((h) => h.currency === cur);
      const known = mine.filter((h) => h.cost_basis != null);
      const book = known.reduce((sum, h) => sum + h.cost_basis, 0);
      const value = known.reduce((sum, h) => sum + (h.value || 0), 0);
      return { book, gain: value - book, pct: book > 0 ? ((value - book) / book) * 100 : null, missing: mine.length - known.length, known: known.length };
    };
    const gainText = (gain, pct, cur, short = false) => `${gain > 0 ? "+" : gain < 0 ? MINUS : ""}${(short ? moneyShort : money)(Math.abs(gain), cur)}${pct != null ? ` (${pct >= 0 ? "+" : MINUS}${Math.abs(pct).toFixed(1)}%)` : ""}`;
    function openPosition(h) {
      const history = investmentActivity(() => ({ account_id: h.account_id, security_id: h.security_id }), false, id);
      const avg = h.cost_basis != null && h.quantity > 0 ? h.cost_basis / h.quantity : null;
      openSheet(
        el("h2", { text: h.ticker || h.name }),
        h.ticker && el("p", { class: "muted", text: h.name }),
        el("div", { class: "row" }, el("div", { class: "grow", text: "Quantity" }), el("div", { class: "amount", text: String(h.quantity) })),
        el("div", { class: "row" }, el("div", { class: "grow", text: "Book value" }), el("div", { class: "amount", text: h.cost_basis != null ? money(h.cost_basis, h.currency) : "Not reported by your bank" })),
        avg != null && el("div", { class: "row" }, el("div", { class: "grow", text: "Average cost per unit" }), el("div", { class: "amount", text: money(avg, h.currency) })),
        el("div", { class: "row" }, el("div", { class: "grow", text: "Value" }), el("div", { class: "amount", text: h.value != null ? money(h.value, h.currency) : "\u2014" })),
        h.gain != null && el("div", { class: "row" }, el("div", { class: "grow", text: "Gain" }),
          el("div", { class: `amount ${h.gain > 0 ? "income" : h.gain < 0 ? "neg" : ""}`, text: gainText(h.gain, h.cost_basis > 0 ? (h.gain / h.cost_basis) * 100 : null, h.currency) })),
        el("h3", { text: "Buys, sells and dividends" }),
        history.node,
        el("div", { class: "actions" }, el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })));
      history.load(true);
    }
    const paint = (data) => {
      const totals = Object.entries(data.totals);
      const status = data.status;
      lastData = data;
      // Only say something when prices are off or failing; the usual "live, about every two minutes" note is gone.
      const statusLine = !status.enabled
        ? "Live prices are turned off on the server. Showing your bank's last prices."
        : status.message
          ? (status.live_positions > 0
            ? `Live prices for ${status.live_positions} of ${status.positions} positions so far. Yahoo Finance is having trouble (${status.message}); the rest follow when it allows.`
            : `Live prices unavailable (${status.message}). Showing your bank's last prices.`)
          : status.live_positions === 0
            ? "No position has a live price. Showing your bank's last prices."
            : "";
      const head = el("section", { class: "card" }, el("h3", { text: "Total value" }),
        totals.length === 0 ? el("p", { class: "empty", text: "No investment holdings yet. They appear once an investment account is linked and synced." })
          : el("div", { class: "cur-boxes" }, totals.map(([cur, t]) => {
            // Each currency is its own bordered box, so it is clear which figures belong together.
            const c = changeText(t.changes[invPeriod], cur);
            const b = bookOf(data.holdings, cur);
            return el("div", { class: "cur-box", role: "group", "aria-label": `${cur} total` },
              el("div", { class: "cur-name", text: cur }),
              // The value and the (small) book value share a line; each gain line is its label first, then the figures.
              el("div", { class: "cur-top" }, el("span", { class: "cur-value", text: moneyShort(t.value, cur) }),
                b.known > 0 && el("span", { class: "cur-book muted", text: `Book ${moneyShort(b.book, cur)}` })),
              b.known > 0 && el("div", { class: "cur-line" }, el("span", { class: "muted", text: `All time${b.missing > 0 ? ` \u00B7 ${b.missing} without book value` : ""}` }),
                el("strong", { class: b.gain > 0 ? "income" : b.gain < 0 ? "neg" : "", text: gainText(b.gain, b.pct, cur, true) })),
              c ? el("div", { class: "cur-line" }, el("span", { class: "muted", text: upperFirst(PERIOD_HELP[invPeriod]) }), el("strong", { class: c.cls, text: c.text }))
                : el("div", { class: "cur-line muted", text: "No change available for this period yet." }));
          })),
        statusLine && el("p", { class: `${status.message || !status.enabled ? "error" : "muted"} inv-note`, text: statusLine }));

      const byAccount = new Map();
      for (const h of data.holdings) {
        if (!byAccount.has(h.account_id)) byAccount.set(h.account_id, []);
        byAccount.get(h.account_id).push(h);
      }
      const cards = [...byAccount].map(([accountId, holdings]) => el("section", { class: "card" },
        // The bank's logo sits left of the account's name on one line (no account number).
        el("h2", { class: "acct-title" }, bankLogo((banks.get(accountId) || {}).item, (banks.get(accountId) || {}).name, 24), el("span", { text: holdings[0].account_name })),
        [...new Set(holdings.map((h) => h.currency))].map((cur) => {
          const b = bookOf(holdings, cur);
          const v = holdings.filter((h) => h.currency === cur).reduce((sum, h) => sum + (h.value || 0), 0);
          return el("p", { class: "muted" }, `Value ${moneyShort(v, cur)}`, b.known > 0 && ` \u00B7 Book value ${moneyShort(b.book, cur)} \u00B7 `,
            b.known > 0 && el("span", { class: b.gain > 0 ? "income" : b.gain < 0 ? "neg" : "", text: gainText(b.gain, b.pct, cur, true) }));
        }),
        // Positions are a small table: the ticker, what the position is worth, its all-time P&L %, and its P&L % over the period chosen above.
        (() => {
          const periodLabel = (CHANGE_PERIODS.find(([key]) => key === invPeriod) || [, invPeriod])[1];
          const sign = (n) => (n > 0 ? "+" : n < 0 ? MINUS : "");
          const pctCell = (pct) => el("td", { class: `num ${pct == null || pct === 0 ? "" : pct > 0 ? "income" : "neg"}`, text: pct == null ? "\u2014" : `${sign(pct)}${Math.abs(pct).toFixed(2)}%` });
          const open = (h) => openPosition(h);
          return el("div", { class: "table-wrap" }, el("table", { class: "acct-table pos-table" },
            el("thead", {}, el("tr", {}, [["Ticker", ""], ["Position", "num"], ["Currency", ""], ["All time %", "num"], [`${periodLabel} %`, "num"]]
              .map(([text, cls]) => el("th", { scope: "col", class: cls, text })))),
            el("tbody", {}, holdings.map((h) => {
              const allTime = h.gain != null && h.cost_basis > 0 ? (h.gain / h.cost_basis) * 100 : null;
              const change = h.changes[invPeriod];
              return el("tr", { class: "click", tabindex: 0, onclick: () => open(h),
                onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(h); } } },
                el("td", { class: "pos-ticker", title: h.live_note || h.name }, el("strong", { text: h.ticker || h.name })),
                el("td", { class: "num", text: h.value != null ? h.value.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "\u2014" }),
                el("td", { class: "pos-cur muted", text: h.currency }),
                pctCell(allTime), pctCell(change && change.percent != null ? change.percent : null));
            }))));
        })()));
      view().replaceChildren(viewerNote(), head, ...cards);   // a position's own buys, sells and dividends are in its detail sheet
    };

    let banksLoaded = false;
    const load = async () => {
      const [data] = await Promise.all([api(scoped("/api/holdings/live"), { timeout: 40000 }), banksLoaded ? null : loadBanks()]);
      banksLoaded = true;
      paint(data);
    };
    await load();
    if (id !== navId) return;
    timer = setInterval(() => {
      if (document.hidden || id !== navId) return;
      load().catch((err) => { if (err instanceof SignedOut) showLogin(); });
    }, 60000);
    cleanups.push(() => clearInterval(timer));
  }

  // ---------------------------------------------------------------- Investments: Performance

  const PERF_RANGES = [["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["ytd", "YTD"], ["1y", "1Y"], ["3y", "3Y"], ["5y", "5Y"]];
  let perfRange = "1y";
  let perfBench = "sp500";
  let perfRiskFree = 3;   // percent a year; the server's default is 3

  // Plain-language meaning of each figure, shown when you tap its name (and as a hover title).
  const PERF_HELP = {
    total_return: "How much the positions grew (or shrank) over the period, from the first day shown to the last.",
    cash_effect: "Your return with cash minus the return of the invested part alone, in percentage points. Negative: cash held the result back. Positive: it cushioned a fall.",
    cash_share: "How much of the money is cash.",
    cash_value: "The cash held today, in the base currency.",
    cash_invested: "What the investments alone returned over the period, leaving the cash out.",
    cagr: "Total return turned into a yearly rate, so periods of different lengths can be compared. Left blank under 30 days: it would mean nothing.",
    volatility: "How much the value swings day to day, scaled to a year. Higher means a bumpier ride. Typical stock funds sit around 12–20%.",
    sharpe: "Return above the risk-free rate for each unit of volatility. Higher is better; above 1 is good, below 0.5 is weak. It lets you compare a calm portfolio with a wild one fairly.",
    sortino: "Like Sharpe, but only counts the bad swings (days that lose money). Better than Sharpe when most of the swings are upward.",
    max_drawdown: "The biggest fall from a high point to the low after it. It is the worst loss someone who bought at the top would have sat through.",
    calmar: "Yearly return divided by the biggest fall. Shows how much return you got for the worst pain.",
    var_95: "On 95% of days the loss was smaller than this. A guide to a normally bad day, not the worst one.",
    cvar_95: "The average loss on the worst 5% of days. A guide to how bad the bad days get.",
    positive_days: "The share of days the value went up.",
    positive_months: "The share of calendar months the value went up.",
    beta: "How much your positions move when the benchmark moves 1%. Under 1 is calmer than the benchmark, over 1 swings more, near 0 moves on its own.",
    alpha: "Yearly return beyond what the benchmark's moves explain (Jensen's alpha). Positive means the picks added something. Over short periods this is mostly noise.",
    correlation: "How closely your positions follow the benchmark, from -1 to 1. Near 1 means they move together, so the benchmark adds little diversification.",
    r_squared: "The share of your ups and downs explained by the benchmark. High means you hold something like the benchmark.",
    tracking_error: "How far your returns stray from the benchmark's, as a yearly figure. Near 0 means you are tracking it closely.",
    information_ratio: "Return above the benchmark for each unit of tracking error. Above 0.5 is good; it rewards beating the benchmark steadily.",
    excess_return: "Your total return minus the benchmark's over the same days.",
    up_capture: "Of the benchmark's good months, the share of the gain you kept. Over 100% is more than the benchmark.",
    down_capture: "Of the benchmark's bad months, the share of the fall you took. Under 100% means you fell less. Aim for high up-capture and low down-capture.",
  };

  const pctPlain = (v, digits = 1) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
  const pctSigned = (v, digits = 1) => (v == null ? "—" : `${v > 0 ? "+" : v < 0 ? MINUS : ""}${Math.abs(v * 100).toFixed(digits)}%`);
  const ratio = (v) => (v == null ? "—" : v.toFixed(2));
  const tone = (v) => (v == null || v === 0 ? "" : v > 0 ? "income" : "neg");

  // A two-line (you / benchmark) chart over dates. `valueText(v)` words a value for the readout and axis.
  function perfLineChart({ dates, series, axis, readout, area = false, label, marks = [] }) {
    const W = chartW(), phone = W < 640, H = 230, L = phone ? 60 : 52, R = 12, T = 12, B = phone ? 30 : 26;
    const n = dates.length;
    const wrap = el("div");
    if (n < 2) return el("p", { class: "empty", text: "Not enough history for this chart yet." });
    const x = (i) => L + (i / (n - 1)) * (W - L - R);
    const all = series.flatMap((s) => s.values);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (area) hi = Math.max(hi, 0);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad;
    if (!area) hi += pad;
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const chart = svg("svg", { class: "chart perf-chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
    for (let i = 0; i <= 3; i++) {
      const v = lo + ((hi - lo) * i) / 3;
      chart.append(svg("line", { class: "gl", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      const t = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" });
      t.textContent = axis(v);
      chart.append(t);
    }
    const path = (values) => values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    for (const s of series) {
      if (area) chart.append(svg("path", { class: `area ${s.cls}`, d: `${path(s.values)} L${x(n - 1)},${y(0)} L${L},${y(0)} Z` }));
      chart.append(svg("path", { class: `line ${s.cls}`, d: path(s.values) }));
    }
    if (lo < 0 && hi > 0 && !area) chart.append(svg("line", { class: "gl zero", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
    for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
      const t = svg("text", { x: x(i), y: H - 7, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" });
      t.textContent = parseDay(dates[i]).toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "2-digit" });
      chart.append(t);
    }
    for (const m of marks) {   // a dot on the first line where something happened, with its note on hover
      let i = dates.findIndex((d) => d >= m.date);
      if (i < 0) i = n - 1;
      const mark = svg("circle", { class: "mark", cx: x(i), cy: y(series[0].values[i]), r: 4.5 });
      const tip = svg("title");
      tip.textContent = m.label;
      mark.append(tip);
      chart.append(mark);
    }
    const dots = series.map((s) => svg("circle", { r: 3.5, class: `dot ${s.cls}` }));
    chart.append(...dots);
    const caption = el("p", { class: "perf-readout" });
    const key = el("div", { class: "perf-key" }, series.map((s) => el("span", { class: "perf-chip" },
      svg("svg", { width: 22, height: 10, "aria-hidden": "true" }, svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, class: `line ${s.cls}` })), s.name)));
    function show(i) {
      series.forEach((s, k) => { dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(s.values[i])); });
      caption.replaceChildren(el("strong", { text: dayLabel(dates[i]) }), ...series.map((s) => el("span", {}, `${s.name} `, el("strong", { text: readout(s.values[i]) }))));
    }
    chart.addEventListener("pointermove", (event) => {
      const box = chart.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W;
      show(Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1)))));
    });
    chart.addEventListener("pointerleave", () => show(n - 1));
    wrap.append(key, chart, caption);
    show(n - 1);
    return wrap;
  }

  // Monthly returns: a pair of bars per month (you, benchmark), gains up and falls down from one zero line.
  function monthlyBars(rows, benchName) {
    if (rows.length === 0) return el("p", { class: "empty", text: "No full months in this period yet." });
    const W = chartW(), phone = W < 640, H = phone ? 220 : 210, L = 46, R = 8, T = 12, B = phone ? 30 : 26;
    const peak = Math.max(0.01, ...rows.flatMap((r) => [Math.abs(r.portfolio ?? 0), Math.abs(r.benchmark ?? 0)]));
    const y = (v) => T + (1 - (v + peak) / (2 * peak)) * (H - T - B);
    const chart = svg("svg", { class: "chart perf-chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Return for each month, you against the benchmark" });
    for (const v of [-peak, 0, peak]) {
      chart.append(svg("line", { class: v === 0 ? "gl zero" : "gl", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      const t = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" });
      t.textContent = pctSigned(v, 0).replace("+", "");
      chart.append(t);
    }
    const slot = (W - L - R) / rows.length;
    const nth = everyNth(rows.length, W - L - R, phone ? 34 : 28);
    const bar = (v, cls, at, width, tip) => {
      if (v == null) return;
      const top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
      const rect = svg("rect", { class: `bar ${cls}`, x: at, y: top, width, height: h, rx: 2 });
      const title = svg("title");
      title.textContent = tip;
      rect.append(title);
      chart.append(rect);
    };
    rows.forEach((r, i) => {
      const left = L + i * slot + slot * 0.1, width = slot * 0.4;
      const name = `${monthLabel(r.month)}${r.partial ? " (part month)" : ""}`;
      bar(r.portfolio, "p-you", left, width, `${name}: you ${pctSigned(r.portfolio)}`);
      bar(r.benchmark, "p-bench", left + width + 1, width, `${name}: ${benchName} ${pctSigned(r.benchmark)}`);
      if (i % nth === 0) {
        const t = svg("text", { x: L + i * slot + slot / 2, y: H - 8, "text-anchor": "middle" });
        t.textContent = shortMonth(r.month);
        chart.append(t);
      }
    });
    return chart;
  }

  const ALLOC_COLOURS = ["var(--chart)", "var(--c-assets)", "var(--c-liab)", "var(--c-inv)", "var(--muted)", "var(--muted-2)"];
  function allocationBar(title, rows) {
    return el("div", { class: "alloc" },
      el("h4", { text: title }),
      el("div", { class: "alloc-bar", role: "img", "aria-label": rows.map((r) => `${r.name} ${pctPlain(r.share)}`).join(", ") },
        rows.map((r, i) => { const part = el("i", { title: `${r.name}: ${pctPlain(r.share)}` }); part.style.flex = String(Math.max(r.share, 0.002)); part.style.background = ALLOC_COLOURS[i % ALLOC_COLOURS.length]; return part; })),
      el("ul", { class: "alloc-list" }, rows.map((r, i) => {
        const swatch = el("i", { class: "alloc-swatch" });
        swatch.style.background = ALLOC_COLOURS[i % ALLOC_COLOURS.length];
        return el("li", {}, swatch, el("span", { class: "grow", text: r.name }), el("span", { class: "num", text: pctPlain(r.share) }));
      })));
  }

  async function viewPerformance(id) {
    $("page-sub").textContent = "How the positions have done against a benchmark";
    let data = null;
    const rangeSeg = el("div", { class: "seg wide period-seg", role: "group", "aria-label": "Period" },
      PERF_RANGES.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === perfRange), text: label,
        onclick: () => { perfRange = key; reload(); } })));
    const benchSelect = el("select", { "aria-label": "Compare with", onchange: (e) => { perfBench = e.target.value; reload(); } });
    const fillBench = (list) => {
      benchSelect.replaceChildren(...list.map((b) => el("option", { value: b.key, text: b.name, selected: b.key === perfBench })));
    };
    fillBench([{ key: perfBench, name: perfBench === "sp500" ? "S&P 500" : perfBench }]);
    const bar = $("subtabs");
    bar.replaceChildren(invTabs(), rangeSeg);
    bar.hidden = false;
    measureBars();
    const syncRange = () => rangeSeg.querySelectorAll("button").forEach((btn, i) => btn.setAttribute("aria-pressed", String(PERF_RANGES[i][0] === perfRange)));

    const reload = async () => {
      syncRange();
      view().replaceChildren(viewerNote(), el("p", { class: "empty", text: "Working out performance…" }));
      try { await load(); } catch (err) { if (err instanceof SignedOut) showLogin(); else if (id === navId) view().replaceChildren(el("p", { class: "empty error", role: "alert", text: err.message })); }
    };

    const metric = (key, label, text, cls = "") => el("div", { class: "perf-tile", title: PERF_HELP[key] || "" },
      el("span", { class: "muted", text: label }), el("strong", { class: cls, text }));

    const paint = () => {
      const d = data;
      if (!d.available) {
        view().replaceChildren(viewerNote(), perfSections(), el("section", { class: "card" }, el("h3", { text: "Performance" }), el("p", { class: "empty", text: d.message || "Performance is not available right now." })));
        return;
      }
      fillBench(d.benchmarks);
      const bn = d.benchmark.name;
      const m = d.metrics, bm = d.benchmark_metrics || {}, rel = d.relative || {};
      const hasBench = !!d.benchmark_index;

      const note = el("section", { class: "card perf-assume" },
        el("p", { class: "muted", text: `${d.assumption} Price changes only: deposits, withdrawals and dividends are not counted, so this shows how the positions you hold now have behaved, not what your account actually earned.` }),
        el("label", { class: "perf-rf" }, "Compare with", benchSelect),
        el("label", { class: "perf-rf" }, "Risk-free rate",
          el("input", { type: "number", min: 0, max: 20, step: 0.1, value: String(perfRiskFree), "aria-label": "Risk-free rate, percent a year",
            onchange: (e) => { const v = Number(e.target.value); if (Number.isFinite(v) && v >= 0 && v <= 20) { perfRiskFree = v; reload(); } } }),
          el("span", { class: "muted perf-note", text: "% a year, used for Sharpe, Sortino and alpha" })),
        hasBench && !d.benchmark.converted && el("p", { class: "error", text: `${bn} is quoted in ${d.benchmark.quoted_in} and no exchange rate was available, so it is shown unconverted.` }),
        !hasBench && el("p", { class: "error", text: `No price history was available for ${bn}, so there is nothing to compare against.` }));

      const cov = d.coverage;
      const coverage = (cov.unpriced.length || cov.missing_rates.length || cov.started_later || cov.priced_share < 0.99) && el("section", { class: "card" },
        el("h3", { text: "What is included" }),
        el("p", { class: "muted", text: `${pctPlain(cov.priced_share, 0)} of your money (${cov.priced_positions} of ${cov.positions} positions) has price history.${cov.started_later ? ` The chart starts on ${shortDate(cov.start)}, when enough of it does, instead of ${shortDate(cov.requested_start)}.` : ""}` }),
        cov.unpriced.length > 0 && el("p", { class: "muted", text: `Left out for lack of prices: ${cov.unpriced.join(", ")}.` }),
        cov.missing_rates.length > 0 && el("p", { class: "error", text: `No exchange rate for ${cov.missing_rates.join(", ")}, so those positions are left out.` }));

      const tiles = el("div", { class: "perf-tiles" },
        metric("total_return", "Total return", pctSigned(m.total_return), tone(m.total_return)),
        metric("cagr", "Per year", pctSigned(m.cagr), tone(m.cagr)),
        metric("volatility", "Volatility", pctPlain(m.volatility)),
        metric("sharpe", "Sharpe", ratio(m.sharpe)),
        metric("max_drawdown", "Biggest fall", pctSigned(m.max_drawdown), tone(m.max_drawdown)),
        hasBench && metric("excess_return", `vs ${bn}`, pctSigned(rel.excess_return), tone(rel.excess_return)),
        d.cash && d.cash.effect != null && metric("cash_effect", "Cash effect", `${d.cash.effect > 0 ? "+" : d.cash.effect < 0 ? MINUS : ""}${Math.abs(d.cash.effect * 100).toFixed(1)} pts`, tone(d.cash.effect)));

      const growth = el("section", { class: "card" }, el("h3", { text: "Growth of 100" }),
        perfLineChart({ dates: d.dates, label: "Growth of 100: your positions against the benchmark",
          series: [{ name: "You", cls: "p-you", values: d.portfolio }, ...(hasBench ? [{ name: bn, cls: "p-bench", values: d.benchmark_index }] : []),
            ...(d.invested_index ? [{ name: "Without cash", cls: "p-invested", values: d.invested_index }] : [])],
          axis: (v) => v.toFixed(0), readout: (v) => v.toFixed(1) }),
        el("p", { class: "muted small", text: `All lines start at 100 on the first day, so the gap between them is the difference in growth.${d.invested_index ? " \u201CWithout cash\u201D is the invested part alone: the gap to \u201CYou\u201D is what holding cash did." : ""}` }));

      const down = el("section", { class: "card" }, el("h3", { text: "Falls from the high" }),
        perfLineChart({ dates: d.dates, area: true, label: "How far below its previous high each line was",
          series: [{ name: "You", cls: "p-you", values: d.portfolio_drawdown }, ...(hasBench ? [{ name: bn, cls: "p-bench", values: d.benchmark_drawdown }] : [])],
          axis: (v) => `${(v * 100).toFixed(0)}%`, readout: (v) => pctPlain(v) }),
        m.drawdown && m.drawdown.peak && el("p", { class: "muted small", text: `Your biggest fall was ${pctPlain(Math.abs(m.drawdown.depth))}, from ${shortDate(m.drawdown.peak)} to ${shortDate(m.drawdown.trough)}${m.drawdown.recovered ? `, back at the old high by ${shortDate(m.drawdown.recovered)}.` : ", and it has not yet got back to that high."}` }));

      const rows = [
        ["Total return", "total_return", pctSigned, true], ["Per year", "cagr", pctSigned, true], ["Volatility", "volatility", pctPlain, false],
        ["Sharpe ratio", "sharpe", ratio, true], ["Sortino ratio", "sortino", ratio, true], ["Biggest fall", "max_drawdown", pctSigned, true],
        ["Return vs biggest fall (Calmar)", "calmar", ratio, true], ["Bad-day loss (95%)", "var_95", pctPlain, false], ["Worst-5%-day average loss", "cvar_95", pctPlain, false],
        ["Days up", "positive_days", (v) => pctPlain(v, 0), true], ["Months up", "positive_months", (v) => pctPlain(v, 0), true],
      ];
      const score = el("section", { class: "card" }, el("h3", { text: "Scorecard" }),
        el("p", { class: "muted small", text: "Tap a name for what it means. A tick marks the better figure." }),
        el("div", { class: "table-wrap" }, el("table", { class: "acct-table perf-table" },
          el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Measure" }), el("th", { scope: "col", class: "num", text: "You" }), hasBench && el("th", { scope: "col", class: "num", text: bn }))),
          el("tbody", {}, rows.map(([label, key, fmt, higher]) => {
            const you = m[key], bench = bm[key];
            const better = hasBench && you != null && bench != null && you !== bench ? (higher ? you > bench : you < bench) : null;
            const help = el("tr", { class: "perf-help", hidden: true }, el("td", { colspan: hasBench ? 3 : 2, class: "muted small", text: PERF_HELP[key] }));
            const head = el("tr", { class: "click", tabindex: 0, "aria-expanded": "false", onclick: () => { help.hidden = !help.hidden; head.setAttribute("aria-expanded", String(!help.hidden)); },
              onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); head.click(); } } },
              el("td", { text: label }),
              el("td", { class: "num" }, better === true && el("span", { class: "tick", "aria-label": "better", text: "✓ " }), fmt(you)),
              hasBench && el("td", { class: "num" }, better === false && el("span", { class: "tick", "aria-label": "better", text: "✓ " }), fmt(bench)));
            return [head, help];
          }).flat()))));

      const vs = hasBench && el("section", { class: "card" }, el("h3", { text: `Against ${bn}` }),
        el("div", { class: "perf-tiles" },
          metric("beta", "Beta", ratio(rel.beta)), metric("alpha", "Alpha / year", pctSigned(rel.alpha), tone(rel.alpha)),
          metric("correlation", "Correlation", ratio(rel.correlation)), metric("r_squared", "R²", ratio(rel.r_squared)),
          metric("tracking_error", "Tracking error", pctPlain(rel.tracking_error)), metric("information_ratio", "Information ratio", ratio(rel.information_ratio)),
          metric("up_capture", "Up capture", pctPlain(rel.up_capture, 0)), metric("down_capture", "Down capture", pctPlain(rel.down_capture, 0))),
        el("p", { class: "muted small", text: "Hold or tap a figure for what it means. Beta, alpha and capture need a few months of history to be meaningful." }));

      const months = el("section", { class: "card" }, el("h3", { text: "Month by month" }),
        el("div", { class: "perf-key" }, el("span", { class: "perf-chip" }, el("i", { class: "swatch p-you" }), "You"), hasBench && el("span", { class: "perf-chip" }, el("i", { class: "swatch p-bench" }), bn)),
        monthlyBars(d.monthly.slice(-24), bn),
        m.best_month && el("p", { class: "muted small", text: `Best month ${monthLabel(m.best_month)} (${pctSigned(m.best_month_return)}), worst ${monthLabel(m.worst_month)} (${pctSigned(m.worst_month_return)}).` }));

      const posTable = el("section", { class: "card" }, el("h3", { text: "Which positions drove it" }),
        el("div", { class: "table-wrap" }, el("table", { class: "acct-table perf-table" },
          el("thead", {}, el("tr", {}, [["Position", ""], ["Weight", "num"], ["Return", "num"], ["Added", "num"], ["Volatility", "num"], ["Fall", "num"]].map(([t, c]) => el("th", { scope: "col", class: c, text: t })))),
          el("tbody", {}, d.positions.map((p) => el("tr", {},
            el("td", { title: p.name }, el("strong", { text: p.ticker || p.name }), p.cash ? (p.ticker && el("span", { class: "muted small", text: "  cash" })) : !p.priced && el("span", { class: "muted small", text: "  no price history" })),
            el("td", { class: "num", text: pctPlain(p.weight) }),
            el("td", { class: `num ${tone(p.return)}`, text: pctSigned(p.return) }),
            el("td", { class: `num ${tone(p.contribution)}`, text: pctSigned(p.contribution) }),
            el("td", { class: "num", text: pctPlain(p.volatility) }),
            el("td", { class: `num ${tone(p.max_drawdown)}`, text: pctSigned(p.max_drawdown) })))))),
        el("p", { class: "muted small", text: "Added is how many percentage points of the total return came from that position." }));

      const c = d.concentration;
      const spread = el("section", { class: "card" }, el("h3", { text: "How it is spread" }),
        el("div", { class: "perf-tiles" },
          el("div", { class: "perf-tile", title: "Share of the money in the single largest position." }, el("span", { class: "muted", text: "Largest position" }), el("strong", { text: pctPlain(c.top1, 0) })),
          el("div", { class: "perf-tile", title: "Share in the three largest positions." }, el("span", { class: "muted", text: "Top 3" }), el("strong", { text: pctPlain(c.top3, 0) })),
          el("div", { class: "perf-tile", title: "How many equal-sized positions would be as concentrated as this. Lower than the number you hold means a few dominate." }, el("span", { class: "muted", text: "Effective holdings" }), el("strong", { text: c.effective_holdings == null ? "—" : c.effective_holdings.toFixed(1) }))),
        allocationBar("By type", d.allocation.by_type), d.allocation.by_currency.length > 1 && allocationBar("By currency", d.allocation.by_currency), d.allocation.by_account.length > 1 && allocationBar("By account", d.allocation.by_account));

      const cashCard = d.cash && el("section", { class: "card" }, el("h3", { text: "Cash" }),
        el("div", { class: "perf-tiles" },
          metric("cash_share", "Share of the money", pctPlain(d.cash.share, 1)),
          metric("cash_value", "Cash held", moneyShort(d.cash.value, d.currency)),
          d.cash.invested_return != null && metric("cash_invested", "Invested part alone", pctSigned(d.cash.invested_return), tone(d.cash.invested_return)),
          d.cash.effect != null && metric("cash_effect", "Effect of cash", `${d.cash.effect > 0 ? "+" : d.cash.effect < 0 ? MINUS : ""}${Math.abs(d.cash.effect * 100).toFixed(1)} pts`, tone(d.cash.effect))),
        el("p", { class: "muted small", text: "Cash is counted in every figure on this page, so it dilutes the return, the swings and the falls like any other choice. The effect is your return with cash minus the return of the invested part alone: negative means cash held the result back over this period, positive means it cushioned a fall." }),
        Object.entries(d.cash.interest || {}).map(([k, v]) => el("p", { class: "muted small", text: `Interest in the last 12 months: ${moneyShort(v.amount, k)}${v.yield != null ? ` (${pctPlain(v.yield, 2)} of the ${k} cash)` : ""}. It is not in the price-only figures above.` })));

      const incomeCards = Object.entries(d.income || {});
      const income = incomeCards.length > 0 && el("section", { class: "card" }, el("h3", { text: "Income and fees, last 12 months" }),
        incomeCards.map(([k, v]) => el("div", { class: "perf-income" },
          el("div", { class: "cur-name", text: k }),
          el("div", { class: "row" }, el("div", { class: "grow", text: "Dividends" }), el("div", { class: "amount income", text: `+${moneyShort(v.dividends, k)}` })),
          v.interest > 0 && el("div", { class: "row" }, el("div", { class: "grow", text: "Interest" }), el("div", { class: "amount income", text: `+${moneyShort(v.interest, k)}` })),
          el("div", { class: "row" }, el("div", { class: "grow", text: "Fees" }), el("div", { class: "amount", text: v.fees > 0 ? `${MINUS}${moneyShort(v.fees, k)}` : moneyShort(0, k) })),
          v.dividend_yield != null && el("p", { class: "muted small", text: `Dividends are ${pctPlain(v.dividend_yield, 2)} of the value${v.fee_share ? `; fees ${pctPlain(v.fee_share, 2)}` : ""}.` }))));

      const guide = d.guidance.length > 0 && el("section", { class: "card" }, el("h3", { text: "Things to consider" }),
        el("div", { class: "guidance" }, d.guidance.map((g) => el("div", { class: `guide guide-${g.level}` },
          el("strong", {}, el("span", { class: "guide-mark", "aria-hidden": "true", text: g.level === "good" ? "✓" : g.level === "watch" ? "!" : "i" }), el("span", { class: "visually-hidden", text: `${g.level}: ` }), g.title),
          el("p", { text: g.text })))),
        el("p", { class: "muted small", text: "General observations from the numbers above, not personal financial advice. Past results do not predict future ones." }));

      view().replaceChildren(viewerNote(), perfSections(), note, tiles, growth, down, score, ...(vs ? [vs] : []), months, posTable, spread, ...(cashCard ? [cashCard] : []), ...(income ? [income] : []), ...(guide ? [guide] : []), ...(coverage ? [coverage] : []));
    };

    const load = async () => {
      const q = new URLSearchParams({ range: perfRange, benchmark: perfBench, risk_free: String(perfRiskFree) });
      const next = await api(scoped(`/api/performance?${q}`), { timeout: 120000 });
      if (id !== navId) return;
      data = next;
      paint();
    };
    await load();
  }

  // ---------------------------------------------------------------- Investments: Performance > What ifs

  const WI_RANGES = [["3m", "3M"], ["6m", "6M"], ["1y", "1Y"], ["all", "All"]];
  const WI_VERDICT = { missed_gain: "Missed gain", good_call: "Good call", even: "About even" };
  let wiRange = "1y";
  let wiSmall = false;          // also show sales under the minimum
  let wiFollow = true;          // follow the money you were paid into what it bought (otherwise: as cash that stayed put)
  const wiOff = new Set();      // sales switched off in the what-if (kept for the visit, not saved)

  async function viewWhatIfs(id) {
    $("page-sub").textContent = "What if you had not sold?";
    let data = null;
    const rangeSeg = el("div", { class: "seg wide period-seg", role: "group", "aria-label": "Sales from the last" },
      WI_RANGES.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === wiRange), text: label,
        onclick: () => { wiRange = key; reload(); } })));
    const bar = $("subtabs");
    bar.replaceChildren(invTabs(), rangeSeg);
    bar.hidden = false;
    measureBars();
    const syncRange = () => rangeSeg.querySelectorAll("button").forEach((btn, i) => btn.setAttribute("aria-pressed", String(WI_RANGES[i][0] === wiRange)));
    const reload = async () => {
      syncRange();
      view().replaceChildren(viewerNote(), perfSections(), el("p", { class: "empty", text: "Playing out your sales…" }));
      try { await load(); } catch (err) { if (err instanceof SignedOut) showLogin(); else if (id === navId) view().replaceChildren(el("p", { class: "empty error", role: "alert", text: err.message })); }
    };

    const paint = () => {
      const d = data;
      if (!d.available) {
        const small = d.coverage && d.coverage.hidden_small > 0;
        view().replaceChildren(viewerNote(), perfSections(), el("section", { class: "card" }, el("h3", { text: "What ifs" }),
          el("p", { class: "empty", text: d.message || "This is not available right now." }),
          small && smallToggle(d)));
        return;
      }
      const cur = d.currency;
      const sells = d.sells;
      const priced = sells.filter((s) => s.priced);
      const chosen = () => priced.filter((s) => !wiOff.has(s.id));
      const sign = (v) => (v > 0 ? "+" : v < 0 ? MINUS : "");
      const gapText = (v) => `${sign(v)}${money(Math.abs(v), cur)}`;
      // The what-if is the sales that are ticked: each one's kept value less what the money did, summed per date.
      const gapSeries = () => {
        const out = new Array(d.dates.length).fill(0);
        for (const s of chosen()) s.kept_path.forEach((k, j) => { out[s.start + j] += k - s.proceeds_path[j]; });
        return out;
      };

      const hero = el("section", { class: "card wi-hero" });
      const chartBox = el("div");
      const drawHero = () => {
        const picked = chosen();
        const gaps = gapSeries();
        const gap = gaps.length ? gaps[gaps.length - 1] : 0;
        const proceeds = picked.reduce((sum, s) => sum + (s.proceeds || 0), 0);
        const pct = proceeds > 0 ? gap / proceeds : null;
        const almost = pct != null && Math.abs(pct) <= 0.005;
        const words = picked.length === 0 ? "Tick at least one sale below to play it out."
          : almost ? "Keeping what you sold would have left you about where you are."
          : gap > 0 ? `Keeping what you sold would have left you ${money(gap, cur)} better off today.`
          : `Selling was right: keeping it would have left you ${money(-gap, cur)} worse off today.`;
        hero.replaceChildren(
          el("h3", { text: `If you had kept ${picked.length === sells.length ? "everything" : `${picked.length} of ${sells.length} sales`} you sold` }),
          el("div", { class: "wi-figure" },
            el("strong", { class: `wi-big ${almost ? "" : tone(gap)}`, text: picked.length ? gapText(gap) : "—" }),
            pct != null && el("span", { class: "muted", text: `${pctSigned(pct)} of the ${money(proceeds, cur)} you were paid, as it stands today` })),
          el("p", { text: words }));
        const marks = picked.map((s) => ({ date: s.date, label: `${shortDate(s.date)}: sold ${s.quantity} ${s.ticker || s.name}` }));
        const series = d.actual
          ? [{ name: "What happened", cls: "p-you", values: d.actual }, { name: "If you had kept them", cls: "p-invested", values: d.actual.map((a, i) => a + gaps[i]) }]
          : [{ name: "Kept, less what happened to the money", cls: "p-you", values: gaps }];
        chartBox.replaceChildren(perfLineChart({
          dates: d.dates, series, marks, label: "Your investment accounts as they were, against as they would have been had you kept what you sold",
          axis: (v) => money(v, cur, { notation: "compact", maximumFractionDigits: 1 }), readout: (v) => money(v, cur) }));
      };

      const wentInto = (s) => (s.proceeds_mode === "followed" || s.proceeds_mode === "partly"
        ? s.followed.map((f) => `${f.ticker || f.name} on ${shortDate(f.date)}${f.share < 0.995 ? ` (${Math.round(f.share * 100)}%)` : ""}`).join(", ") + (s.proceeds_mode === "partly" ? ", the rest as cash" : "")
        : "");
      const open = (s) => {
        const sliced = d.dates.slice(s.start);
        openSheet(
          el("h2", { text: `${s.ticker || s.name}, sold ${shortDate(s.date)}` }),
          el("div", { class: "row" }, el("div", { class: "grow", text: "Sold" }), el("div", { class: "amount", text: `${s.quantity} at ${s.price != null ? money(s.price, s.currency) : "—"}` })),
          el("div", { class: "row" }, el("div", { class: "grow", text: "You were paid" }), el("div", { class: "amount", text: money(s.proceeds, cur) })),
          el("div", { class: "row" }, el("div", { class: "grow", text: "If you had kept it, now" }), el("div", { class: "amount", text: money(s.kept_now, cur) })),
          el("div", { class: "row" }, el("div", { class: "grow", text: s.proceeds_mode === "cash" ? "The money as cash, now" : "What the money became, now" }), el("div", { class: "amount", text: money(s.proceeds_now, cur) })),
          el("div", { class: "row" }, el("div", { class: "grow", text: "Where the money went" }), el("div", { class: "amount wi-went", text: wentInto(s) ? `Bought ${wentInto(s)}` : "Held as cash" })),
          el("div", { class: "row" }, el("div", { class: "grow", text: "Difference" }),
            el("div", { class: `amount ${tone(s.gap)}`, text: `${gapText(s.gap)} (${pctSigned(s.gap_percent)}) · ${WI_VERDICT[s.verdict]}` })),
          perfLineChart({ dates: sliced, label: "What the shares would be worth against the money as cash",
            series: [{ name: "If kept", cls: "p-you", values: s.kept_path }, { name: s.proceeds_mode === "cash" ? "As cash" : "What the money did", cls: "p-bench", values: s.proceeds_path }],
            axis: (v) => money(v, cur, { notation: "compact", maximumFractionDigits: 1 }), readout: (v) => money(v, cur) }),
          el("p", { class: "muted small", text: `The shares are worth what you were paid on the day, then rise and fall with the price. ${s.proceeds_mode === "cash" ? "The cash is held as it was (it moves only with the exchange rate)." : "The money is followed into what you bought with it; any part not matched to a purchase stays cash."}` }),
          el("div", { class: "actions" }, el("button", { type: "button", class: "secondary", text: "Close", onclick: closeSheet })));
      };

      const rows = [...sells].reverse().map((s) => {
        const head = el("div", { class: "grow" },
          el("div", {}, el("strong", { text: s.ticker || s.name }), el("span", { class: "muted", text: `  ${s.quantity} @ ${s.price != null ? moneyShort(s.price, s.currency) : "—"}` })),
          el("div", { class: "muted small", text: `${shortDate(s.date)} · ${s.account_name} · paid ${s.proceeds != null ? money(s.proceeds, cur) : "—"}${wentInto(s) ? ` · into ${wentInto(s)}` : ""}` }));
        if (!s.priced) {
          return el("div", { class: "row wi-row wi-off" }, el("span", { class: "wi-check-gap" }), head, el("div", { class: "muted small wi-result", text: `Not played out: ${s.reason}` }));
        }
        const check = el("input", { type: "checkbox", class: "wi-check", checked: !wiOff.has(s.id), "aria-label": `Include the ${s.ticker || s.name} sale of ${shortDate(s.date)} in the what-if`,
          onchange: (e) => { if (e.target.checked) wiOff.delete(s.id); else wiOff.add(s.id); drawHero(); } });
        const result = el("div", { class: "wi-result" },
          el("strong", { class: tone(s.gap), text: gapText(s.gap) }),
          el("div", { class: "muted small", text: `${pctSigned(s.gap_percent)} · ${WI_VERDICT[s.verdict]}` }));
        const body = el("div", { class: "wi-body click", role: "button", tabindex: 0, onclick: () => open(s),
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(s); } } }, head, result);
        return el("div", { class: "row wi-row" }, check, body);
      });

      const totals = d.totals;
      const followSeg = el("div", { class: "wi-follow" },
        el("span", { class: "muted small", text: "What happened to the money you were paid" }),
        el("div", { class: "seg compact", role: "group", "aria-label": "What happened to the money you were paid" },
          [[true, "Follow it"], [false, "As cash"]].map(([on, label]) => el("button", { type: "button", "aria-pressed": String(on === wiFollow), text: label,
            title: on ? "Follow the money into what you bought with it, when you bought in the same account within 5 days" : "Treat it as cash that stayed put",
            onclick: () => { if (on !== wiFollow) { wiFollow = on; reload(); } } }))));
      const list = el("section", { class: "card" }, el("h3", { text: "Your sales" }),
        followSeg,
        el("p", { class: "muted small", text: "Tick the sales to include. Untick one to see the picture without it; tap a sale for its own chart." }),
        el("div", { class: "perf-tiles" },
          el("div", { class: "perf-tile" }, el("span", { class: "muted", text: "Missed gains" }), el("strong", { text: String(totals.missed) })),
          el("div", { class: "perf-tile" }, el("span", { class: "muted", text: "Good calls" }), el("strong", { text: String(totals.good) })),
          el("div", { class: "perf-tile" }, el("span", { class: "muted", text: "About even" }), el("strong", { text: String(totals.even) }))),
        el("div", { class: "wi-list" }, rows));

      const cov = d.coverage;
      const notes = el("section", { class: "card" }, el("h3", { text: "How to read this" }),
        el("p", { class: "muted small", text: d.assumption }),
        el("p", { class: "muted small", text: "It is hindsight: of course it looks better to have held what went up. It also leaves out the dividends those shares would have paid, any tax on the sale, and the price you actually got, so use it to learn from the pattern, not to second-guess one sale. Only sales from about the last two years are known." }),
        d.proceeds === "auto" && el("p", { class: "muted small", text: "Following the money is a guess from timing: a purchase within 5 days of a sale, in the same account, is taken as where the money went. It is not a record of where it came from, so check it against what you remember." }),
        !d.actual && el("p", { class: "muted small", text: "The chart shows only the difference: the value of your accounts on past days could not be worked out." }),
        cov.hidden_small > 0 && smallToggle(d));
      view().replaceChildren(viewerNote(), perfSections(), hero, el("section", { class: "card" }, chartBox), list, notes);
      drawHero();
    };

    const smallToggle = (d) => el("p", { class: "muted small" },
      d.coverage.hidden_small > 0 ? `${d.coverage.hidden_small} sale${d.coverage.hidden_small === 1 ? "" : "s"} under ${money(d.min_amount, d.currency)} ${d.coverage.hidden_small === 1 ? "is" : "are"} hidden. ` : "",
      el("button", { type: "button", class: "link", text: wiSmall ? "Hide small sales" : "Show small sales", onclick: () => { wiSmall = !wiSmall; reload(); } }));

    const load = async () => {
      const next = await api(scoped(`/api/whatifs?range=${wiRange}&min_amount=${wiSmall ? 0 : 50}&proceeds=${wiFollow ? "auto" : "cash"}`), { timeout: 120000 });
      if (id !== navId) return;
      data = next;
      paint();
    };
    await load();
  }

  // ---------------------------------------------------------------- Merchants

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
      // The Merchants pages sit in the reserved bar, under the Spending tabs, outside the part of the page that scrolls.
      const bar = $("subtabs");
      for (const old of bar.querySelectorAll(".merch-tabs")) old.remove();
      bar.append(el("div", { class: "toolbar merch-tabs" }, tabs));
      measureBars();
      view().replaceChildren(viewerNote(true), merchTab === "needs" ? todoCard : masterCard);
      if (merchTab === "merchants") draw();
    }
    show();
  }

  async function openAddMerchant(done) {
    let choices;
    try { choices = await categoryChoices(); } catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); return; }
    const name = el("input", { maxlength: 80, placeholder: "e.g. Corner Store", "aria-label": "Merchant name" });
    const category = optionalCategoryPicker(choices, null);
    const income = incomeSwitch(false);
    const message = el("p", { class: "error", role: "alert" });
    openSheet(el("h2", { text: "Add a merchant" }),
      el("p", { class: "muted", text: "Creates an empty merchant. Attach bank text to it afterwards with Name it." }),
      el("label", {}, "Name", name),
      el("h3", { text: "Category (optional)" }), category.node,
      income.node, el("p", { class: "muted", text: INCOME_NOTE }), message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Add", onclick: async () => {
          try { await api("/api/merchants", { method: "POST", body: { name: name.value, category: category.value(), income: income.value() } }); closeSheet(); toast("Merchant added."); done(); }
          catch (err) { if (err instanceof SignedOut) { closeSheet(); showLogin(); } else message.textContent = err.message; }
        } }),
        el("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeSheet })));
  }

  // Give some bank text a name. `done` runs after it is saved.
  async function openNameDialog({ pattern = "", field = "name", kind = "equals", name = "" }, done) {
    let existing = [], choices = [];
    try { existing = (await api("/api/merchants?period=all")).merchants.map((m) => m.name); choices = await categoryChoices(); } catch (err) { if (err instanceof SignedOut) { showLogin(); return; } }
    const categoryPick = optionalCategoryPicker(choices, null);
    const incomePick = incomeSwitch(false);
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
      el("div", { class: "field-row" }, el("span", { class: "muted", text: "When" }), fieldSel, kindSel),
      el("div", { class: "field-row" }, el("label", {}, "Bank text", patternInput)),
      previewBox,
      datalist,
      el("div", { class: "field-row" }, el("label", { class: "grow-2" }, "Merchant", nameInput), el("label", {}, "Category", categoryPick.node)),
      el("p", { class: "muted", text: "Pick an existing merchant name to combine them. Automatic uses a matching rule if there is one, otherwise the category from your bank." }),
      incomePick.node, el("p", { class: "muted", text: `${INCOME_NOTE} Leave it off to keep what the merchant has now.` }),
      message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Save name", onclick: async () => {
          message.textContent = "";
          try {
            const r = await api("/api/merchants/aliases", { method: "POST", body: { match_field: fieldSel.value, match_type: kindSel.value, pattern: patternInput.value, name: nameInput.value, ...(categoryPick.value() ? { category: categoryPick.value() } : {}), ...(incomePick.value() ? { income: true } : {}) } });
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
        stat("Last seen", d.last_date ? shortDate(d.last_date) : "\u2014"),
        stat("Category", d.category ? categoryLabel(d.category) : "Automatic"),
        stat("Money in", d.income ? "Counted as income" : "Automatic")),
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
        el("button", { type: "button", text: "Edit", onclick: () => openRename(d, () => { done(); again(); }) }),
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

  async function openRename(d, done) {
    let choices;
    try { choices = await categoryChoices(); } catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); return; }
    const name = el("input", { value: d.name, maxlength: 80, "aria-label": "Merchant name" });
    const category = optionalCategoryPicker(choices, d.category);
    const income = incomeSwitch(d.income);
    const message = el("p", { class: "error", role: "alert" });
    openSheet(el("h2", { text: "Edit merchant" }),
      el("p", { class: "muted", text: "The new name shows everywhere. Matching transactions stay attached, including future ones." }),
      el("label", {}, "Name", name),
      el("h3", { text: "Category" }),
      el("p", { class: "muted", text: "All this merchant\u2019s transactions use it, including future ones, unless a rule or a change you made by hand says otherwise. Automatic uses a matching rule if there is one, otherwise the category from your bank." }),
      category.node,
      el("h3", { text: "Money coming in" }), income.node, el("p", { class: "muted", text: INCOME_NOTE }), message,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", text: "Save", onclick: async () => {
          try { await api(`/api/merchants/${d.id}`, { method: "PATCH", body: { name: name.value, category: category.value(), income: income.value() } }); toast("Saved."); done(); }
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
    idleHold = true;
    window.Plaid.create({
      token: linkToken,
      onSuccess: async (publicToken) => {
        idleHold = false;
        reportActivity();
        try {
          if (item) {
            await api("/api/link/complete", { method: "POST", body: { item_id: item.id } });
            toast(`${item.institution || "Bank"} reconnected. It is syncing now.`);
          } else {
            const added = await api("/api/link/exchange", { method: "POST", body: { public_token: publicToken } });
            toast(`${added.institution || "Bank"} connected. The first sync runs in the background.`);
          }
          syncPolls = 0;
          refreshStatus();   // the count on the cog drops straight away, not at the next page change
          done();
        } catch (err) {
          if (err instanceof SignedOut) showLogin(); else toast(err.message, true);
        }
      },
      onExit: (err) => { idleHold = false; reportActivity(); if (err) toast(err.display_message || err.error_message || "Linking was cancelled.", true); },
    }).open();
  }

  // Colour scheme and accent. Kept in this browser only (see theme.js).
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
              "aria-label": accent, "aria-pressed": String(t.accent === accent), onclick: () => choose("accent", accent) })))));
    };
    paint();
    return card;
  }

  // Settings has a few groups, each with its own tabs (a group with one tab shows no second row).
  const SETTINGS_GROUPS = [
    ["data", "App data", [["banks", "Banks"], ["people", "People"], ["categories", "Categories"], ["rules", "Rules"], ["loans", "Loans"]]],
    ["security", "Security", [["logins", "Logins"], ["passkeys", "Passkeys"], ["devices", "Devices"], ["log", "Sign-in log"], ["backups", "Backups"]]],
    ["look", "Appearance", [["appearance", "Appearance"]]],
  ];
  const SETTINGS_TABS = SETTINGS_GROUPS.flatMap(([, , tabs]) => tabs);
  const groupOf = (tab) => SETTINGS_GROUPS.find(([, , tabs]) => tabs.some(([key]) => key === tab)) || SETTINGS_GROUPS[0];
  let settingsTab = "banks";

  async function viewSettings(id, params = new URLSearchParams()) {
    if (SETTINGS_TABS.some(([key]) => key === params.get("tab"))) settingsTab = params.get("tab");
    const [items, sessions, rules, accounts, choices, log, backups, logins, release, passkeyList, loanKinds] = await Promise.all([
      api("/api/items"), api("/auth/sessions"), api("/api/rules"), api("/api/accounts"), categoryChoices(), api("/auth/login-log?limit=50"),
      api("/api/backups").catch(() => ({ backups: [], last_daily: null, keep: {} })), api("/auth/users").catch(() => []),
      api("/api/version").catch(() => null), api("/api/passkeys").catch(() => []), loanTypes()]);
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

    if (passkeyList.length === 0) rememberPasskey(false);  // nothing registered, so nothing on this device either
    const passkeyName = el("input", { value: guessDevice(), maxlength: "60", "aria-label": "Name for this passkey" });
    const passkeyPassword = el("input", { type: "password", autocomplete: "current-password", "aria-label": "Your password" });
    passkeyPassword.addEventListener("focus", () => { if (passkeysSupported()) prefetchRegisterOptions().catch(() => {}); });  // ready by the time you tap
    const passkeyMessage = el("p", { class: "error", role: "alert" });
    const passkeysCard = el("section", { class: "card" }, el("h2", { text: "Passkeys" }),
      el("p", { class: "muted", text: "Sign in with your device's unlock (Face ID, fingerprint or PIN) instead of a password and code. Each passkey is tied to this address. Your password and authenticator code keep working, and a passkey that syncs through iCloud works on your other Apple devices." }),
      passkeyList.length === 0 && el("p", { class: "empty", text: "No passkeys yet." }),
      passkeyList.map((p) => el("div", { class: "row" }, el("div", { class: "grow" },
        el("strong", { text: p.name }),
        el("div", { class: "muted", text: `Added ${ago(p.created_at)} \u00B7 ${p.last_used_at ? `last used ${ago(p.last_used_at)}` : "not used yet"}` })),
        el("button", { type: "button", class: "danger", text: "Remove", onclick: async () => {
          if (!(await confirmBox({ title: `Remove ${p.name}?`, intro: "That device will need your password and authenticator code to sign in.",
            confirmText: "Remove", cancelText: "Cancel", danger: true }))) return;
          try { await api(`/api/passkeys/${encodeURIComponent(p.id)}`, { method: "DELETE" }); reload(); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
        } }))),
      passkeysSupported()
        ? el("div", { class: "actions passkey-add" }, el("label", {}, "Name", passkeyName), el("label", {}, "Your password (to confirm)", passkeyPassword),
          el("button", { type: "button", class: "primary", text: "Add a passkey on this device", onclick: async () => {
            passkeyMessage.textContent = "";
            if (!passkeyPassword.value) { passkeyMessage.textContent = "Enter your password to confirm."; return; }
            try { await addPasskey(passkeyName.value.trim() || guessDevice(), passkeyPassword.value); toast("Passkey added"); reload(); }
            catch (err) { if (err instanceof SignedOut) showLogin(); else passkeyMessage.textContent = err.message; }
          } }), passkeyMessage)
        : el("p", { class: "muted", text: "This browser cannot make passkeys. Open HomeFinance in Safari or Chrome on your phone." }));

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
    const accountsOf = new Map();
    for (const a of accounts) accountsOf.set(a.item_id, [...(accountsOf.get(a.item_id) || []), a]);
    const orphans = accounts.filter((a) => !items.some((i) => i.id === a.item_id));

    // A bank's accounts as a table: the account, who owns it (a column of its own) and a Hide switch (another).
    function accountsTable(group) {
      const hasOwners = people.length > 0;
      const row = (a) => {
        const chosen = new Set(a.owner_ids || []);
        return el("tr", {},
          el("td", { class: "col-account" }, el("strong", { text: a.name }), a.mask && el("span", { class: "muted", text: `  \u2022\u2022${a.mask}` })),
          hasOwners && el("td", { class: "col-owners" }, el("div", { class: "owners", role: "group", "aria-label": `Owners of ${a.name}` }, people.map((p) =>
            el("label", {}, el("input", { type: "checkbox", checked: chosen.has(p.id), onchange: (e) => {
              if (e.target.checked) chosen.add(p.id); else chosen.delete(p.id);
              putOwners(`/api/accounts/${encodeURIComponent(a.id)}/owners`, [...chosen], () => toast("Saved."));
            } }), p.name)))),
          el("td", { class: "col-hide" }, el("label", { class: "switch", title: "Hide this account from totals and budgets" },
            el("input", { type: "checkbox", role: "switch", "aria-label": `Hide ${a.name}`, checked: a.hidden, onchange: async (e) => {
              try { await api(`/api/accounts/${encodeURIComponent(a.id)}`, { method: "PATCH", body: { hidden: e.target.checked } }); toast(e.target.checked ? "Hidden." : "Shown."); }
              catch (err) { e.target.checked = !e.target.checked; if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
            } }), el("span", { class: "track", "aria-hidden": "true" }))));
      };
      return el("div", { class: "table-wrap" }, el("table", { class: "bank-accounts" },
        el("thead", {}, el("tr", {}, el("th", { scope: "col", class: "col-account", text: "Account" }),
          hasOwners && el("th", { scope: "col", class: "col-owners", text: "Owners" }),
          el("th", { scope: "col", class: "col-hide", text: "Hide" }))),
        el("tbody", {}, group.map(row))));
    }

    // A bank's details (who owns what, Hide) start folded away so the list of banks stays short; the arrow opens them.
    // The banks you open stay open while the page reloads itself after a change (until the page is loaded afresh).
    function foldButton(body, what, key) {
      const paint = () => {
        button.setAttribute("aria-expanded", String(!body.hidden));
        button.setAttribute("aria-label", `${body.hidden ? "Show" : "Hide"} ${what}`);
        button.textContent = body.hidden ? "\u25B8" : "\u25BE";
      };
      const button = el("button", { type: "button", class: "twisty", onclick: () => {
        body.hidden = !body.hidden;
        if (body.hidden) openBanks.delete(key); else openBanks.add(key);
        paint();
      } });
      body.hidden = !openBanks.has(key);
      paint();
      return button;
    }

    // One block per bank: the bank and its status, then (folded) its accounts with who owns them and Hide.
    function bankBlock(i) {
      const group = accountsOf.get(i.id) || [];
      const name = i.institution || "this bank";
      const ownersPath = `/api/items/${encodeURIComponent(i.id)}/owners`;
      const details = el("div", { class: "bank-body", hidden: true },
        people.length > 0 && group.length > 0 && el("div", { class: "bank-owners" }, el("span", { class: "muted", text: "Set every account here to" }),
          people.map((p) => el("button", { type: "button", class: "small", text: p.name,
            onclick: () => putOwners(ownersPath, [p.id], () => { toast(`Every ${name} account is now ${p.name}'s.`); reload(); }) })),
          people.length > 1 && el("button", { type: "button", class: "small", text: "Shared by everyone",
            onclick: () => putOwners(ownersPath, people.map((p) => p.id), () => { toast("Shared by everyone."); reload(); }) })),
        group.length > 0 && accountsTable(group),
        group.length === 0 && el("p", { class: "muted bank-note", text: i.last_synced_at ? "No accounts at this bank." : "Its accounts appear after the first sync." }));
      return el("div", { class: "bank-block" },
        el("div", { class: "row bank-line" }, foldButton(details, `the accounts at ${name}`, i.id), bankLogo(i, i.institution, 36), el("div", { class: "grow" }, el("strong", { text: i.institution || "Bank" }),
          el("div", { class: i.needs_reauth ? "error" : "muted", text: i.needs_reauth ? "Needs reconnecting" : i.last_synced_at ? `Last synced ${ago(i.last_synced_at)}` : "Waiting for the first sync\u2026" })),
          i.needs_reauth && el("button", { type: "button", class: "primary", text: "Reconnect", onclick: () => startLink(i, reload) }),
          el("button", { type: "button", class: "danger bank-remove", text: "Remove", onclick: async () => {
            if (!(await confirmBox({
              title: `Remove ${name}?`,
              intro: "This deletes the bank's accounts, transactions and holdings from your server, and closes the connection at Plaid.",
              points: ["The Plaid bank slot it used is not given back.", "If the bank only needs attention, use Reconnect instead."],
              confirmText: "Remove bank", cancelText: "Keep it", danger: true }))) return;
            try { await api(`/api/items/${encodeURIComponent(i.id)}`, { method: "DELETE" }); toast(`${name} removed.`); refreshStatus(); reload(); }
            catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
          } })),
        details);
    }

    // Removing a bank is hidden until you press Edit, so it takes a deliberate extra click.
    const editButton = el("button", { type: "button", text: "Edit", "aria-pressed": "false", onclick: () => {
      const on = !banks.classList.contains("editing");
      banks.classList.toggle("editing", on);
      editButton.textContent = on ? "Done" : "Edit";
      editButton.setAttribute("aria-pressed", String(on));
    } });
    const banks = el("section", { class: "card banks-card" },
      el("div", { class: "card-head" }, el("h2", { text: "Banks" }), el("div", { class: "actions" }, linkButton, syncButton, items.length > 0 && editButton)),
      syncOut,
      // With people set up there is nothing to explain; without any, say where to add them so accounts can have owners.
      people.length === 0 && el("p", { class: "muted", text: "To say who owns what, add the people in your household on the People tab first." }),
      unowned > 0 && el("p", { class: "muted", text: `${unowned} account${unowned === 1 ? " has" : "s have"} no owner yet. They count in the household but not in anyone's own view.` }),
      items.length === 0 && el("p", { class: "empty", text: "No banks linked yet. Press Link a bank to add your first." }),
      items.map(bankBlock),
      // An account whose bank is not in the list (for example just after a bank was removed) must never vanish from Hide and owners.
      orphans.length > 0 && (() => {
        const details = el("div", { class: "bank-body", hidden: true }, accountsTable(orphans));
        return el("div", { class: "bank-block" },
          el("div", { class: "row bank-line" }, foldButton(details, "the other accounts", "other"), el("div", { class: "grow" }, el("strong", { text: "Other accounts" }),
            el("div", { class: "muted", text: "Not linked to a bank listed above." }))),
          details);
      })());

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

    // ---- Categories: the built-in ones plus the ones you add, each with its sub-categories.
    const mine = new Map(customCategories.map((c) => [c.code, c]));
    const byLabel = (a, b) => categoryLabel(a).localeCompare(categoryLabel(b));
    const addCategoryName = el("input", { placeholder: "New category, e.g. Pets", maxlength: 50, "aria-label": "New category name" });
    const subParent = el("select", { "aria-label": "Parent category" },
      [...PRIMARIES].sort(byLabel).map((c) => el("option", { value: c, text: categoryLabel(c) })));
    const addSubName = el("input", { placeholder: "New sub-category, e.g. Coffee", maxlength: 50, "aria-label": "New sub-category name" });
    async function addCategory(name, parent, field) {
      if (!name.trim()) { toast("Type a name first.", true); return; }
      try {
        await api("/api/categories", { method: "POST", body: parent ? { name, parent } : { name } });
        toast("Category added."); field.value = ""; reload();
      } catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
    }
    async function removeCategory(code) {
      if (!(await confirmBox({ title: `Remove ${categoryLabel(code)}?`,
        intro: "It is only removed from the list of categories you can choose. It can't be removed while transactions, budgets or rules still use it.",
        confirmText: "Remove category", cancelText: "Keep it", danger: true }))) return;
      try { await api(`/api/categories/${encodeURIComponent(code)}`, { method: "DELETE" }); toast("Category removed."); reload(); }
      catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
    }
    const usage = (c) => { const bits = [c.transactions && `${c.transactions} transaction${c.transactions === 1 ? "" : "s"}`, c.budgets && `${c.budgets} budget${c.budgets === 1 ? "" : "s"}`, c.rules && `${c.rules} rule${c.rules === 1 ? "" : "s"}`, c.merchants && `${c.merchants} merchant${c.merchants === 1 ? "" : "s"}`].filter(Boolean); return bits.length ? bits.join(", ") : "Not used yet"; };
    const categoryRows = [];
    for (const top of [...PRIMARIES].sort(byLabel)) {
      const own = mine.get(top);
      categoryRows.push(el("div", { class: "row" },
        el("div", { class: "icon", text: icon(top), "aria-hidden": "true" }),
        el("div", { class: "grow" }, el("strong", { text: categoryLabel(top) }), el("span", { class: "badge", text: own ? "Yours" : "Built in" }),
          own && el("div", { class: "muted", text: usage(own) })),
        own && el("button", { type: "button", class: "danger", text: "Remove", "aria-label": `Remove ${categoryLabel(top)}`, onclick: () => removeCategory(top) })));
      for (const sub of choices.filter((c) => parentOf(c) === top).sort(byLabel)) {
        const subOwn = mine.get(sub);
        categoryRows.push(el("div", { class: "row sub" },
          el("div", { class: "grow" }, el("span", { text: subLabel(sub) }), subOwn && el("span", { class: "badge", text: "Yours" }),
            subOwn && el("div", { class: "muted", text: usage(subOwn) })),
          subOwn && el("button", { type: "button", class: "danger", text: "Remove", "aria-label": `Remove ${categoryLabel(sub)}`, onclick: () => removeCategory(sub) })));
      }
    }
    const categoriesCard = el("section", { class: "card" }, el("h2", { text: "Categories" }),
      el("p", { class: "muted", text: "Add your own categories and sub-categories. They appear wherever you choose a category: on a transaction, in a rule and in a budget. Spending in a sub-category counts in its category once, so totals are never doubled." }),
      el("h3", { text: "Add a category" }),
      el("div", { class: "toolbar" }, addCategoryName, el("button", { type: "button", class: "primary", text: "Add category", onclick: () => addCategory(addCategoryName.value, null, addCategoryName) })),
      el("h3", { text: "Add a sub-category" }),
      el("div", { class: "toolbar" }, subParent, addSubName, el("button", { type: "button", class: "primary", text: "Add sub-category", onclick: () => addCategory(addSubName.value, subParent.value, addSubName) })),
      el("h3", { text: "All categories" }),
      categoryRows);

    // ---- Backups: copies of the encrypted database, kept on the NAS next to it.
    const BACKUP_KINDS = { daily: "Daily", manual: "Manual", upgrade: "Before an upgrade" };
    const sizeText = (bytes) => (bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);
    const backupButton = el("button", { type: "button", class: "primary", text: "Back up now", onclick: async () => {
      backupButton.disabled = true; backupButton.textContent = "Backing up\u2026";
      try { await api("/api/backup", { method: "POST", timeout: 120000 }); toast("Backup saved."); reload(); }
      catch (err) { backupButton.disabled = false; backupButton.textContent = "Back up now"; if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
    } });
    const backupsCard = el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { text: "Backups" }), backupButton),
      el("p", { class: "muted", text: backups.last_daily
        ? `A copy is taken automatically every day. The last one was ${ago(backups.last_daily)}.`
        : "A copy is taken automatically every day. None has been taken yet." }),
      el("p", { class: "muted", text: `The newest ${backups.keep.daily || 14} daily copies and ${backups.keep.manual || 10} manual ones are kept. A copy is also taken just before the app upgrades its database.` }),
      backups.backups.length === 0 && el("p", { class: "empty", text: "No backups yet. Press Back up now." }),
      backups.backups.map((b) => el("div", { class: "row" },
        el("div", { class: "grow" }, el("strong", { text: BACKUP_KINDS[b.kind] || b.kind }),
          el("div", { class: "muted", text: `${new Date(b.created_at).toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" })} \u00B7 ${sizeText(b.size_bytes)}` })),
        el("button", { type: "button", text: "Download", onclick: async (e) => {
          e.target.disabled = true;
          try { await downloadFile(`/api/backups/${encodeURIComponent(b.name)}`, b.name); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
          finally { e.target.disabled = false; }
        } }))),
      el("p", { class: "muted", text: "Copies are encrypted with your master key and saved in the backups folder inside your data folder. They stay on the NAS, so a disk failure would take them too: download one now and then and keep it somewhere else, along with your master key (a copy is useless without it)." }));

    // ---- Logins: who can sign in. Each has their own password and authenticator; all see everything.
    const loginsCard = el("section", { class: "card" }, el("h2", { text: "Logins" }),
      el("p", { class: "muted", text: "Everyone listed here signs in with their own username, password and authenticator code. They all see everything, including every account. They are separate from the People list, which only labels who owns an account." }),
      logins.map((u) => el("div", { class: "row" }, el("div", { class: "grow" },
        el("strong", { text: u.username }), u.you && el("span", { class: "badge", text: "You" }),
        el("div", { class: "muted", text: `${u.last_sign_in ? `Last signed in ${ago(u.last_sign_in)}` : "Has not signed in yet"} \u00B7 ${u.devices} device${u.devices === 1 ? "" : "s"} signed in` })),
        !u.you && el("button", { type: "button", class: "danger", text: "Remove login", onclick: async () => {
          if (!(await confirmBox({
            title: `Remove ${u.username}'s login?`,
            intro: `${u.username} will be signed out everywhere and can no longer sign in. Accounts and data are not affected.`,
            confirmText: "Remove login", cancelText: "Keep it", danger: true }))) return;
          try { await api(`/auth/users/${u.id}`, { method: "DELETE" }); toast("Login removed."); reload(); }
          catch (err) { if (err instanceof SignedOut) showLogin(); else toast(err.message, true); }
        } }))),
      el("h3", { text: "Add a login" }),
      el("p", { class: "muted", text: "A login is added on the NAS, so only someone with access to it can create one. Over SSH, run:" }),
      el("pre", { class: "code" }, "sudo docker exec -it homefinance python -m app.cli create-user --username NAME"),
      el("p", { class: "muted", text: "It asks for a password (12 or more characters) and shows a QR code to scan with an authenticator app, plus one-time recovery codes to save. Their phone also needs Tailscale to reach the app." }));

    const activity = el("section", { class: "card" }, el("h2", { text: "Sign-in activity" }),
      log.locked
        ? el("p", { class: "error", role: "alert", text: "Sign-in is locked. Unlock it from the NAS: docker exec -it homefinance python -m app.cli unlock" })
        : el("p", { class: "muted", text: `${log.failed_streak} of ${log.max_failed_logins} failed attempts in a row. At ${log.max_failed_logins}, new sign-ins are locked until you unlock them on the NAS. A successful sign-in resets the count.` }),
      log.events.length === 0 && el("p", { class: "empty", text: "Nothing recorded yet." }),
      log.events.map((e) => {
        const [label, bad] = LOGIN_RESULTS[e.result] || [e.result, false];
        const detail = [new Date(e.at).toLocaleString("en-CA"), e.username, e.ip && `from ${e.ip}`, e.origin, e.method === "recovery" && "recovery code", e.device_name].filter(Boolean).join(" \u00B7 ");
        return el("div", { class: "row" }, el("div", { class: "grow" },
          el("div", {}, el("strong", { class: bad ? "neg" : "", text: label }), e.count > 1 && el("span", { class: "badge", text: `\u00D7${e.count}` })),
          el("div", { class: "muted", text: detail })));
      }));

    // ---- Loans: what kind of loan each loan account is, which is how the Overview groups what you owe.
    const itemName = new Map(items.map((i) => [i.id, i.institution]));
    const loanAccounts = accounts.filter((a) => a.type === "loan");
    const loansCard = el("section", { class: "card" }, el("h2", { text: "Loans" }),
      el("p", { class: "muted", text: "Say what kind of loan each one is (mortgage, student, car, and so on). The Overview lists what you owe by kind. Automatic goes by what the bank calls the loan." }),
      loanAccounts.length === 0 && el("p", { class: "empty", text: "No loan accounts are linked." }),
      loanAccounts.map((a) => el("div", { class: "row" }, el("div", { class: "grow" },
        el("strong", { text: a.name }), el("div", { class: "muted", text: `${a.institution || itemName.get(a.item_id) || "Bank"}${a.mask ? ` \u2022\u2022${a.mask}` : ""} \u00B7 ${a.current_balance == null ? "no balance" : money(a.current_balance, a.currency)} owed` }),
        loanKindControl(a, loanKinds))
      )));

    const sections = { loans: [loansCard], banks: [banks], people: [peopleCard], rules: [rulesCard], categories: [categoriesCard], appearance: [appearanceCard()], logins: [loginsCard], passkeys: [passkeysCard], devices: [devices], backups: [backupsCard], log: [activity] };
    const versionLine = el("p", { class: "muted version-line", text: release ? `HomeFinance version ${release.version}${release.build ? ` (build ${release.build})` : ""}` : "HomeFinance version unknown" });
    const lastTab = Object.fromEntries(SETTINGS_GROUPS.map(([key, , tabs]) => [key, tabs[0][0]]));  // the tab last open in each group
    const groupBar = el("div", { class: "seg wide", role: "group", "aria-label": "Settings sections" });
    const tabBar = el("div", { class: "tabs-sub", role: "group", "aria-label": "Settings pages" });
    const choose = (tab) => { settingsTab = tab; show(); };
    function show() {
      const [groupKey, , groupTabs] = groupOf(settingsTab);
      lastTab[groupKey] = settingsTab;
      groupBar.replaceChildren(...SETTINGS_GROUPS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === groupKey), text: label,
        onclick: () => choose(lastTab[key]) })));
      tabBar.replaceChildren(...groupTabs.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === settingsTab), text: label, onclick: () => choose(key) })));
      tabBar.hidden = groupTabs.length < 2;
      // The tabs live in the reserved bar under the top bar, outside the part of the page that scrolls.
      const bar = $("subtabs");
      bar.replaceChildren(el("div", { class: "toolbar" }, groupBar), tabBar);
      bar.hidden = false;
      measureBars();
      view().replaceChildren(...sections[settingsTab], versionLine);
    }
    show();
  }

  // ---------------------------------------------------------------- shell: routes, sign-in

  const cleanups = [];
  // Spending is one page with three tabs. The old address #/merchants still opens the right tab, and Transactions is a page of its own.
  const SPENDING_TABS = [
    ["summary", "This month", viewSpendingSummary, "This month against your budgets"],
    ["cashflow", "Cash flow", viewCashflowTab, "Income and expenses over time"],
    ["merchants", "Merchants", viewMerchants, "Where your money goes, over time"],
  ];
  let spendingTab = "summary";
  const isSpendingTab = (key) => SPENDING_TABS.some(([k]) => k === key);
  function viewSpending(id, params) {
    const [, , run, subtitle] = SPENDING_TABS.find(([key]) => key === spendingTab);
    $("page-sub").textContent = subtitle;
    return run(id, params);
  }
  // The tab bar lives outside the page content, so each tab can redraw itself without losing it.
  function renderSubtabs(name) {
    const bar = $("subtabs");
    // Settings fills the bar itself once its data has loaded.
    if (name !== "spending") { bar.hidden = true; bar.replaceChildren(); measureBars(); return; }
    bar.replaceChildren(el("div", { class: "seg", role: "group", "aria-label": "Spending sections" },
      SPENDING_TABS.map(([key, label]) => el("button", { type: "button", "aria-pressed": String(key === spendingTab), text: label,
        onclick: () => { location.hash = `#/spending?tab=${key}`; } }))));
    bar.hidden = false;
    const active = bar.querySelector("[aria-pressed=true]");
    if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest", inline: "center" });
    measureBars();
  }

  // The sticky tab bars sit just under the top bar (and under the Spending tabs), so their heights are measured, not guessed.
  // The pinned bars (top bar and the page's own bar) are measured once and then held at that exact height, so
  // nothing that happens while scrolling on a phone (the browser's toolbars sliding away, a resize that only
  // changes the height) can change their size. They are measured again only when their content or the width changes.
  // The bars are re-measured when something inside them changes size (their own, held size is not watched).
  const barWatcher = window.ResizeObserver ? new ResizeObserver(() => measureBars()) : null;
  function measureBars() {
    const root = document.documentElement.style;
    const top = document.querySelector(".topbar"), sub = $("subtabs");
    top.style.height = "";
    sub.style.height = "";
    const topHeight = Math.ceil(top.getBoundingClientRect().height);
    const subHeight = sub.hidden ? 0 : Math.ceil(sub.getBoundingClientRect().height);
    top.style.height = `${topHeight}px`;
    if (!sub.hidden) sub.style.height = `${subHeight}px`;
    root.setProperty("--topbar-h", `${topHeight}px`);
    root.setProperty("--subtabs-h", `${subHeight}px`);
    if (barWatcher) for (const node of [...top.children, ...sub.children]) barWatcher.observe(node);
  }
  let barsWidth = window.innerWidth;
  window.addEventListener("resize", () => { if (window.innerWidth !== barsWidth) { barsWidth = window.innerWidth; measureBars(); } });

  // A little give when the page is pulled down at the top: the space above the tab content stretches (the pinned bars stay put)
  // and springs back when let go. The browser's own bounce is switched off in the CSS, so this is the only one.
  (() => {
    const page = $("view");
    let startX = 0, startY = 0, pull = 0, active = false;
    const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    const set = (px) => { page.style.transform = px ? `translateY(${px}px)` : ""; };
    const release = () => {
      if (!active) return;
      active = false;
      if (reduced()) { page.style.transition = ""; set(0); pull = 0; return; }
      page.style.transition = "transform .45s cubic-bezier(.2, 1.1, .3, 1)";
      set(0); pull = 0;
      setTimeout(() => { page.style.transition = ""; }, 480);
    };
    document.addEventListener("touchstart", (e) => {
      active = false; pull = 0;
      if (e.touches.length !== 1 || window.scrollY > 0 || e.target.closest("dialog, input, select, textarea, .seg, .cf-tiles")) { startY = null; return; }
      startX = e.touches[0].clientX; startY = e.touches[0].clientY; page.style.transition = "";
    }, { passive: true });
    document.addEventListener("touchmove", (e) => {
      if (startY == null || e.touches.length !== 1) return;
      const dy = e.touches[0].clientY - startY, dx = e.touches[0].clientX - startX;
      if (!active && (dy <= 6 || Math.abs(dx) > dy || window.scrollY > 0)) return;
      active = true;
      pull = Math.min(110, dy * 0.5);   // it gets harder to pull the further it goes
      if (pull <= 0) { release(); return; }
      if (e.cancelable) e.preventDefault();
      set(pull);
    }, { passive: false });
    document.addEventListener("touchend", release);
    document.addEventListener("touchcancel", release);
  })();

  // Pressing a page choice (or any header control) and sliding away would start dragging the link, which looks like a highlight
  // without going anywhere. These are for clicking, not dragging, so the drag is cancelled.
  document.addEventListener("dragstart", (e) => {
    const node = e.target && (e.target.closest ? e.target : e.target.parentElement);
    if (node && node.closest("#tabs, .topbar, .sidebar, .menu-list, .seg, .tabs-sub, .subtabs, button")) e.preventDefault();
  });

  // On a phone the page choice (Overview, Accounts, ...) sits at the top, inside the top bar, so it stays put
  // and never competes with the browser's own bottom toolbar. On a larger screen it is the side bar's menu.
  const tabsNav = $("tabs");
  const phoneQuery = window.matchMedia("(max-width: 860px)");
  function placeTabs() {
    if (phoneQuery.matches) document.querySelector(".topbar").append(tabsNav);
    else document.querySelector(".sidebar").insertBefore(tabsNav, document.querySelector(".side-foot"));
    measureBars();
  }
  phoneQuery.addEventListener("change", placeTabs);
  placeTabs();

  const PAGES = {
    overview: ["Overview", "Net worth over time"],
    accounts: ["Accounts", "Balances by bank and account"],
    transactions: ["Transactions", "Search, filter and re-categorize"],
    spending: ["Spending", "This month, cash flow and merchants"],
    budgets: ["Budgets", "Limits and spending by month"],
    investments: ["Investments", "Positions at live prices"],
    settings: ["Settings", "App data, security and appearance"],
  };
  const ROUTES = { overview: viewOverview, accounts: viewAccounts, transactions: viewTransactions, spending: viewSpending, budgets: viewBudgets, investments: viewInvestments, settings: viewSettings };

  const menuButton = $("menu-button");
  const menuList = $("menu-list");

  // The release this page was loaded as. The server may move on while a tab stays open (an update lands on the NAS),
  // and then the page's script is older than the server. The label under the title shows what the page runs, and
  // turns into a Reload button once the server reports a different release.
  const pageRelease = (document.querySelector('meta[name="release"]') || {}).content || "";
  let releaseCheckedAt = 0;
  function showRelease(serverRelease) {
    const label = $("release");
    if (!label) return;
    if (serverRelease && pageRelease && serverRelease !== pageRelease) {
      label.replaceChildren(el("button", { type: "button", class: "link release-reload", text: `v${pageRelease} \u00B7 server is on ${serverRelease}, tap to reload`,
        onclick: () => location.reload() }));
    } else {
      label.textContent = pageRelease ? `v${pageRelease}` : "";
    }
  }
  async function checkRelease() {
    if (Date.now() - releaseCheckedAt < 60000) return;  // at most once a minute
    releaseCheckedAt = Date.now();
    try { showRelease((await api("/api/version")).version); } catch (err) { if (err instanceof SignedOut) throw err; }
  }
  showRelease("");
  // The reconnect count comes from the banks list, on every screen.
  async function refreshStatus() {
    checkRelease().catch(() => {});
    try {
      const items = await api("/api/items");
      setBanner(items.filter((i) => i.needs_reauth).map((i) => i.institution || "Unknown bank"));
      setAlert(await api("/auth/login-log?limit=100").catch(() => null));
    } catch (err) {
      if (err instanceof SignedOut) showLogin();
    }
  }

  async function showEnvironment() {
    try {
      const { plaid_env: env } = await (await plainFetch("/health")).json();
      const badge = $("env-badge");
      // Real banks are the normal state and need no label. Only the sandbox is flagged, because
      // its balances and transactions are fake.
      badge.hidden = env === "production";
      if (env !== "production") {
        badge.textContent = "Sandbox";
        badge.title = "Plaid's sandbox: fake banks and test data";
        badge.className = "env";
      }
    } catch (_) { /* the badge is only a convenience */ }
  }

  // Everything that needs attention shows as one count on the cog (and on its Settings entry)
  // instead of a banner across the top: banks to reconnect, and failed sign-in attempts.
  function setBanner(institutions) {
    notices.banks = institutions || [];
    renderNotices();
  }

  function renderNotices() {
    const reasons = [];
    if (notices.banks.length) reasons.push(`${notices.banks.join(", ")} ${notices.banks.length === 1 ? "needs" : "need"} to be reconnected`);
    if (notices.signin) reasons.push(notices.signin);
    const count = notices.banks.length + (notices.signin ? 1 : 0);
    const text = reasons.join(". ");
    for (const id of ["menu-count", "menu-item-count"]) {
      const badge = $(id);
      badge.hidden = count === 0;
      badge.textContent = String(count);
      badge.title = text;
    }
    menuButton.title = count ? `Settings and sign out \u2014 ${text}` : "Settings and sign out";
    menuButton.setAttribute("aria-label", count ? `Settings and sign out. ${text}` : "Settings and sign out");
    $("menu-list").querySelector("[data-tab=settings]").setAttribute("href",
      notices.banks.length ? "#/settings?tab=banks" : notices.signin ? "#/settings?tab=log" : "#/settings");
  }

  // Opening a page puts the view at its very top, now and again once the page has been drawn (a phone can
  // keep or move the position while the new page is still loading). Refreshing a page keeps where you are.
  let wantTop = false;
  function toTop() {
    wantTop = true;
    window.scrollTo(0, 0);
    const root = document.scrollingElement;
    if (root) root.scrollTop = 0;
  }

  async function route() {
    if (!token && $("shell").hidden) return;
    while (cleanups.length) cleanups.pop()();
    syncPolls = 0;
    const [path, query] = (location.hash.slice(1) || "/overview").split("?");
    const asked = path.replace(/^\//, "");
    const params = new URLSearchParams(query || "");
    let name = asked in ROUTES || isSpendingTab(asked) ? asked : "overview";
    // Old Spending tabs that became pages of their own: their bookmarks go to the new place.
    if (name === "spending" && params.get("tab") === "budgets") name = "budgets";
    // Cash flow used to sit under the Overview: its old addresses (with a range or grouping) open it where it lives now.
    if (name === "overview" && (params.get("range") || params.get("group"))) { name = "spending"; params.set("tab", "cashflow"); }
    if (name === "spending" && params.get("tab") === "transactions") { name = "transactions"; params.delete("tab"); }  // Transactions used to be a Spending tab
    if (isSpendingTab(name)) { params.set("tab", name); name = "spending"; }  // an old address
    if (name === "spending" && isSpendingTab(params.get("tab"))) spendingTab = params.get("tab");
    if (name === "investments") invTab = params.get("tab") === "performance" ? "performance" : "holdings";
    renderSubtabs(name);
    for (const a of document.querySelectorAll("#tabs a, #menu-list a")) {
      if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    menuButton.classList.toggle("current", name === "settings");
    const [title, subtitle] = PAGES[name];
    $("page-sub").textContent = subtitle;
    document.title = `${title} \u00B7 HomeFinance`;
    const id = ++navId;
    view().replaceChildren(el("p", { class: "empty", text: "Loading…" }));
    try {
      // The household list is only awaited the first time; after that it refreshes alongside the page's own
      // requests instead of ahead of them, which saves a round trip on every page change.
      if (peopleLoaded) loadPeople().catch(() => {}); else { await loadPeople(); peopleLoaded = true; }
      if (id !== navId) return;
      await ROUTES[name](id, params);
      if (id === navId && wantTop) { wantTop = false; window.scrollTo(0, 0); requestAnimationFrame(() => { if (id === navId) window.scrollTo(0, 0); }); }
      refreshStatus();
    } catch (err) {
      if (err instanceof SignedOut) showLogin();
      else if (id === navId) view().replaceChildren(el("p", { class: "empty error", role: "alert", text: err.message }),
        el("p", { class: "empty" }, el("button", { type: "button", text: "Try again", onclick: route })));
    }
  }

  function showLogin(message = "") {
    message ||= signOutReason;
    signOutReason = "";
    stopIdleWatch();
    showPasskeyFirst();
    token = null;
    peopleLoaded = false;  // the next person to sign in gets their own household list
    closeSheet();
    $("shell").hidden = true;
    $("login").hidden = false;
    const timedOut = /inactivity|time limit/.test(message);
    $("login-notice").textContent = timedOut ? message : "";
    $("login-message").textContent = timedOut ? "" : message;
    view().replaceChildren();
    notices.banks = []; notices.signin = null; renderNotices();
    // With Face ID offered, focus nothing: focusing the username makes Chrome pop up its saved
    // passwords straight away, on top of the Face ID button.
    const user = document.querySelector("#login-form [name=username]");
    if (user && $("passkey-first").hidden) user.focus();
  }

  function showShell() {
    $("login").hidden = true;
    $("shell").hidden = false;
    startIdleWatch();
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
    const buttons = event.target.querySelectorAll("button");
    const setUp = event.submitter?.id === "password-passkey-submit";  // "Sign in and set up a passkey"
    const password = form.get("password");
    for (const button of buttons) button.disabled = true;
    $("login-message").textContent = "";
    try {
      const response = await plainFetch("/web/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "HomeFinance" },
        body: JSON.stringify({ username: form.get("username"), password, code: form.get("code") }),
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
      if (setUp) offerPasskeySetup(password);
    } catch (err) {
      $("login-message").textContent = err.message;
    } finally { for (const button of buttons) button.disabled = false; }
  });

  const passkeyButton = $("passkey-login");
  showPasskeyFirst();
  passkeyButton.addEventListener("click", async () => {
    passkeyButton.disabled = true;
    $("login-message").textContent = "";
    try {
      takeTokens(await passkeySignIn());
      showShell();
    } catch (err) {
      $("login-message").textContent = err.message;
    } finally { passkeyButton.disabled = false; }
  });

  for (const button of document.querySelectorAll(".refresh-btn")) button.addEventListener("click", route);
  // The cog opens a small menu: Settings and Sign out.
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
  // Opening a page always starts at its top, never where the last page was scrolled to (the back button
  // included). Opening the page you are already on does the same and loads it afresh.
  try { history.scrollRestoration = "manual"; } catch { /* an old browser keeps its own */ }
  window.addEventListener("hashchange", () => { toTop(); route(); });
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("#tabs a, #menu-list a, #subtabs a, .sidebar a");
    if (!a) return;
    toTop();
    if (a.hash && a.hash === location.hash) route();  // same address: no hashchange will follow
  });
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
