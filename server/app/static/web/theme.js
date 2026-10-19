// Applies the saved colour scheme before the page paints, so there is no flash.
// This browser keeps four harmless notes and nothing else: mode, accent colour, density,
// and whether a passkey was added here (so the sign-in page can offer Face ID first). Storage can be blocked (private windows), so every access is guarded.
(() => {
  "use strict";
  const MODES = ["system", "light", "dark"];
  const ACCENTS = ["blue", "teal", "green", "purple", "orange", "rose"];
  const DENSITIES = ["comfortable", "compact"];
  const PASSKEY = ["no", "yes"];
  const KEYS = { mode: "hf-mode", accent: "hf-accent", density: "hf-density", passkey: "hf-passkey" };
  const ALLOWED = { mode: MODES, accent: ACCENTS, density: DENSITIES, passkey: PASSKEY };

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

  const state = { mode: read("mode", "system"), accent: read("accent", "blue"), density: read("density", "comfortable"), passkey: read("passkey", "no") };

  function apply() {
    const root = document.documentElement;
    if (state.mode === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", state.mode);
    root.setAttribute("data-accent", state.accent);
    root.setAttribute("data-density", state.density);
  }
  apply();

  window.HFTheme = {
    MODES, ACCENTS, DENSITIES,
    get: () => ({ ...state }),
    set(change) {
      for (const [part, value] of Object.entries(change)) {
        if (ALLOWED[part] && ALLOWED[part].includes(value)) { state[part] = value; write(part, value); }
      }
      apply();
    },
  };
})();
