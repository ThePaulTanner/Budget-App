// Sign in, then link or reconnect banks with Plaid Link.
// Tokens live only in memory; the web app's sign-in cookie signs you back in after a reload.
(() => {
  let access = null;
  let refresh = null;

  const $ = (id) => document.getElementById(id);
  const message = (text, isError = false) => {
    $("message").textContent = text;
    $("message").className = isError ? "error" : "";
  };

  // If you are already signed in on the web app, its cookie signs this page in too.
  async function cookieRenew() {
    try {
      const response = await fetch("/web/refresh", { method: "POST", headers: { "X-Requested-With": "HomeFinance" } });
      if (!response.ok) return false;
      access = (await response.json()).access_token;
      return true;
    } catch (_) {
      return false;
    }
  }

  async function request(path, { method = "GET", body, auth = true } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (auth && access) headers.Authorization = `Bearer ${access}`;
    let response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    if (response.status === 401 && auth && !refresh && (await cookieRenew())) {
      headers.Authorization = `Bearer ${access}`;
      response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    } else if (response.status === 401 && auth && refresh) {
      const renewed = await fetch("/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (renewed.ok) {
        const tokens = await renewed.json();
        access = tokens.access_token;
        refresh = tokens.refresh_token;
        headers.Authorization = `Bearer ${access}`;
        response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
      }
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  }

  function showSignedIn(signedIn) {
    $("login").hidden = signedIn;
    $("banks").hidden = !signedIn;
  }

  async function loadBanks() {
    const list = $("bank-list");
    list.replaceChildren();
    const items = await request("/api/items");
    if (items.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = "No banks linked yet.";
      list.append(empty);
    }
    for (const item of items) {
      const row = document.createElement("li");
      const label = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = item.institution || "Bank";
      const status = document.createElement("div");
      status.className = item.needs_reauth ? "status warn" : "status";
      status.textContent = item.needs_reauth
        ? "Needs reconnecting"
        : item.last_synced_at
          ? `Last synced ${new Date(item.last_synced_at).toLocaleString()}`
          : "Waiting for the first sync";
      label.append(name, status);
      row.append(label);
      if (item.needs_reauth) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Reconnect";
        button.addEventListener("click", () => openLink(item.id));
        row.append(button);
      }
      list.append(row);
    }
  }

  async function openLink(itemId) {
    try {
      message("");
      const { link_token } = await request("/api/link/token", {
        method: "POST",
        body: itemId ? { item_id: itemId } : {},
      });
      const handler = Plaid.create({
        token: link_token,
        onSuccess: async (publicToken) => {
          try {
            if (itemId) {
              await request("/api/link/complete", { method: "POST", body: { item_id: itemId } });
            } else {
              await request("/api/link/exchange", { method: "POST", body: { public_token: publicToken } });
            }
            message("Bank connected. The first sync runs in the background.");
            await loadBanks();
          } catch (err) {
            message(err.message, true);
          }
        },
        onExit: (err) => {
          if (err) message(err.display_message || err.error_message || "Linking was cancelled.", true);
        },
      });
      handler.open();
    } catch (err) {
      message(err.message, true);
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const tokens = await request("/auth/login", {
        method: "POST",
        auth: false,
        body: {
          username: form.get("username"),
          password: form.get("password"),
          code: form.get("code"),
          device_name: "Web page",
        },
      });
      access = tokens.access_token;
      refresh = tokens.refresh_token;
      event.target.reset();
      message("");
      showSignedIn(true);
      await loadBanks();
    } catch (err) {
      message(err.message, true);
    }
  });

  cookieRenew().then(async (signedIn) => {
    if (!signedIn) return;
    showSignedIn(true);
    try { await loadBanks(); } catch (err) { message(err.message, true); }
  });

  $("link-bank").addEventListener("click", () => openLink(null));
  $("sign-out").addEventListener("click", async () => {
    try { await request("/auth/logout", { method: "POST" }); } catch (_) { /* already signed out */ }
    access = refresh = null;
    showSignedIn(false);
    message("Signed out.");
  });
})();
