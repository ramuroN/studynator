// Farben pro Konto. Aus der Akzentfarbe und der Helligkeit des Hintergrunds
// (0 = schwarz bis 100 = weiss) werden alle Farben der App abgeleitet, Text
// immer mit mindestens 4.5:1 Kontrast. Läuft im <head>, bevor die Seite
// gezeichnet wird; app.js nutzt es für die Vorschau im Farbwähler.
"use strict";

(function () {
  // Wie in style.css; gebraucht, wenn nur ein Teil gewählt ist.
  const LIGHT = {
    scheme: "light", bg: "#f6f5f1", surface: "#ffffff", text: "#1d1f24", muted: "#5d636e", line: "#dddad2",
    accent: "#2f6b4f", due: "#b4561f", new: "#2a62a8", warn: "#a8322a", mark: "#fbe7a6",
    r1: "#a8322a", r2: "#9a6a12", r3: "#2f6b4f", r4: "#2a62a8",
  };
  const DARK = {
    scheme: "dark", bg: "#15171b", surface: "#1e2126", text: "#e8e6e1", muted: "#a0a5ad", line: "#33373e",
    accent: "#5fb38a", due: "#f0955c", new: "#7fb0f0", warn: "#f07a70", mark: "#5a4a14",
    r1: "#f07a70", r2: "#e0b45a", r3: "#5fb38a", r4: "#7fb0f0",
  };
  const KEYS = ["bg", "surface", "text", "muted", "line", "accent", "accent-text", "accent-ink", "due", "new",
    "warn", "flash", "mark", "r1", "r2", "r3", "r4"];

  const hexToRgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const rgbToHex = (rgb) =>
    "#" + rgb.map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => {
    const [x, y] = [hexToRgb(a), hexToRgb(b)];
    return rgbToHex(x.map((v, i) => v + (y[i] - v) * t));
  };
  const luminance = (hex) => {
    const [r, g, b] = hexToRgb(hex).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const contrast = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((p, q) => q - p);
    return (hi + 0.05) / (lo + 0.05);
  };
  const gray = (level) => {
    const v = Math.round(level * 2.55);
    return rgbToHex([v, v, v]);
  };
  const hslToHex = (h, s, l) => {
    s /= 100;
    l /= 100;
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
    return rgbToHex([f(0) * 255, f(8) * 255, f(4) * 255]);
  };
  const hexToHsl = (hex) => {
    const [r, g, b] = hexToRgb(hex).map((v) => v / 255);
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    let h = 0;
    if (d) {
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = (h * 60 + 360) % 360;
    }
    const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
    return [h, s * 100, l * 100];
  };

  // Schrittweise zur Textfarbe hin, bis die Farbe auf allen Flächen lesbar ist.
  const readable = (color, text, grounds, ratio) => {
    for (let step = 0; step <= 10; step += 1) {
      const candidate = mix(color, text, step / 10);
      if (grounds.every((ground) => contrast(candidate, ground) >= ratio)) return candidate;
    }
    return text;
  };

  function palette(accent, background, prefersDark) {
    let vars;
    if (background == null) {
      vars = { ...(prefersDark ? DARK : LIGHT) };
    } else {
      const bg = gray(background);
      const dark = contrast("#ffffff", bg) > contrast("#000000", bg);
      const preset = dark ? DARK : LIGHT;
      // Flächen heben sich ab, aber in die Richtung, die dem Text Kontrast lässt:
      // hell bei hellem Hintergrund, bei dunklem nur, solange es dunkel genug bleibt.
      let surface = mix(bg, "#ffffff", dark ? 0.06 : 0.6);
      if (dark && contrast(preset.text, surface) < 4.5) surface = mix(bg, "#000000", 0.12);
      // Bei mittlerem Grau reichen die Standardfarben nicht: bis Schwarz oder Weiss.
      const text = readable(preset.text, dark ? "#ffffff" : "#000000", [bg, surface], 4.5);
      vars = {
        ...preset, bg, surface, text,
        line: mix(bg, text, 0.16),
        muted: readable(mix(text, bg, 0.45), text, [bg, surface], 4.5),
      };
      // Signalfarben (fällig, neu, Bewertungen) ebenfalls lesbar halten.
      ["due", "new", "warn", "r1", "r2", "r3", "r4"].forEach((key) => {
        vars[key] = readable(preset[key], text, [bg, surface], 4.5);
      });
    }
    if (accent) vars.accent = accent;
    const onAccent = contrast(vars.accent, "#ffffff") >= contrast(vars.accent, "#111111") ? "#ffffff" : "#111111";
    vars["accent-text"] = contrast(vars.accent, onAccent) >= 4.5 ? onAccent : "#000000";
    vars["accent-ink"] = readable(vars.accent, vars.text, [vars.bg, vars.surface], 4.5);
    vars.flash = mix(vars.surface, vars.accent, vars.scheme === "dark" ? 0.22 : 0.14);
    return vars;
  }

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const metas = [...document.querySelectorAll('meta[name="theme-color"]')];
  metas.forEach((meta) => (meta.dataset.original = meta.content));

  function apply(accent, background) {
    const style = document.documentElement.style;
    theme.current = { accent: accent || null, background: background == null ? null : background };
    KEYS.forEach((key) => style.removeProperty(`--${key}`));
    style.removeProperty("color-scheme");
    if (!accent && background == null) {
      metas.forEach((meta) => (meta.content = meta.dataset.original));
      return;
    }
    const vars = palette(accent, background, darkQuery.matches);
    KEYS.forEach((key) => style.setProperty(`--${key}`, vars[key]));
    style.setProperty("color-scheme", vars.scheme);
    metas.forEach((meta) => (meta.content = vars.bg));
  }

  const theme = { apply, palette, contrast, gray, hslToHex, hexToHsl, current: { accent: null, background: null } };
  window.studyTheme = theme;

  const data = document.documentElement.dataset;
  apply(data.accent || null, data.bg === undefined || data.bg === "" ? null : Number(data.bg));
  // Nur der Akzent gewählt: beim Wechsel hell/dunkel neu rechnen.
  darkQuery.addEventListener("change", () => apply(theme.current.accent, theme.current.background));
})();
