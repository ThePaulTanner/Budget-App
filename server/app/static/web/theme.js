// Applies the saved colour scheme before the page paints, so there is no flash.
// This browser keeps three harmless notes and nothing else: mode, accent colour,
// and whether a passkey was added here (so the sign-in page can offer Face ID first). Storage can be blocked (private windows), so every access is guarded.
(() => {
  "use strict";
  const MODES = ["system", "light", "dark"];
  const ACCENTS = ["blue", "teal", "green", "purple", "orange", "rose"];
  const PASSKEY = ["no", "yes"];
  const KEYS = { mode: "hf-mode", accent: "hf-accent", passkey: "hf-passkey" };
  const ALLOWED = { mode: MODES, accent: ACCENTS, passkey: PASSKEY };

  const read = (part, fallback) => {
    try {
      const value = localStorage.getItem(KEYS[part]);
      return ALLOWED[part].includes(value) ? value : fallback;
    } catch (_) {
      return fallback;
    }
  };
  const write = (part, value) => {
    try { localStorage.setItem(KEYS[part], value); } catch (_) { /* the choice still applies until the page closes */ }
  };

  const state = { mode: read("mode", "system"), accent: read("accent", "blue"), passkey: read("passkey", "no") };

  function apply() {
    const root = document.documentElement;
    if (state.mode === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", state.mode);
    root.setAttribute("data-accent", state.accent);
  }
  apply();

  window.HFTheme = {
    MODES, ACCENTS,
    get: () => ({ ...state }),
    set(change) {
      for (const [part, value] of Object.entries(change)) {
        if (ALLOWED[part] && ALLOWED[part].includes(value)) { state[part] = value; write(part, value); }
      }
      apply();
    },
  };
})();
