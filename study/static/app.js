// Wenig JavaScript: Formeln, Rückfragen, Statusabfrage, Lernansicht.
"use strict";

// Ohne JavaScript bleibt der Hochladen-Knopf sichtbar; mit lädt die Auswahl sofort hoch.
document.documentElement.classList.add("js");

function renderMath(root) {
  if (!window.renderMathInElement) return;
  root.querySelectorAll(".math").forEach((el) =>
    window.renderMathInElement(el, {
      delimiters: [
        { left: "\\[", right: "\\]", display: true },
        { left: "$$", right: "$$", display: true },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    })
  );
}

// Formulare mit data-confirm fragen vorher nach.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault();
});

// Dokument in Arbeit: Status abfragen und neu laden, sobald er sich ändert.
function pollDocument(el) {
  const url = el.dataset.poll;
  const state = el.dataset.state;
  const tick = async () => {
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (res.ok) {
        const data = await res.json();
        if (`${data.status}:${data.progress}` !== state) {
          window.location.reload();
          return;
        }
      }
    } catch (e) {
      // Netz kurz weg: einfach weiter versuchen.
    }
    setTimeout(tick, 2500);
  };
  setTimeout(tick, 2500);
}

function setupLearn(card) {
  const answer = card.querySelector(".answer");
  const reveal = card.querySelector(".reveal");
  const ratings = card.querySelector(".ratings");
  const slide = card.querySelector(".slide");
  const started = Date.now();
  const show = () => {
    if (!answer.hidden) return;
    answer.hidden = false;
    ratings.hidden = false;
    reveal.hidden = true;
    if (slide) slide.hidden = false;
    ratings.querySelector(".r3").focus({ preventScroll: true });
  };
  reveal.addEventListener("click", show);
  // Auf dem Handy: Antippen der Karte zeigt die Antwort (Links und Knöpfe nicht).
  card.addEventListener("click", (event) => {
    if (!event.target.closest("a, button, form")) show();
  });
  ratings.addEventListener("submit", (event) => {
    // Nur einmal abschicken; ein zweites Tippen würde sonst nochmals bewerten.
    if (ratings.dataset.sent) {
      event.preventDefault();
      return;
    }
    ratings.dataset.sent = "1";
    ratings.querySelector('[name="duration_ms"]').value = String(Date.now() - started);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea") || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === " " || event.key === "Enter") {
      if (answer.hidden) {
        event.preventDefault();
        show();
      }
    } else if (["1", "2", "3", "4"].includes(event.key) && !answer.hidden) {
      event.preventDefault();
      ratings.querySelector(`.r${event.key}`).click();
    }
  });
}

// Z nimmt die letzte Bewertung zurück (auch auf «Fertig für jetzt»).
function setupUndo(form) {
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select") || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "z" || event.key === "Z") {
      event.preventDefault();
      form.requestSubmit();
    }
  });
}

// Vorschläge prüfen: Übernehmen und Verwerfen ohne Neuladen, dazu Tasten.
function setupSuggestions() {
  const openForms = () => [...document.querySelectorAll("form.suggestion")];
  const acceptAll = document.querySelector("[data-accept-all]");
  let current = null;

  const select = (form) => {
    if (current) current.classList.remove("current");
    current = form || null;
    if (current) {
      current.classList.add("current");
      current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };
  const step = (delta) => {
    const forms = openForms();
    if (!forms.length) return;
    const index = forms.indexOf(current);
    if (index < 0) select(forms[0]);
    else select(forms[Math.min(forms.length - 1, Math.max(0, index + delta))]);
  };
  // Ausblenden statt entfernen: «Rückgängig» kann wieder offene Vorschläge bringen.
  const updateCount = (open) => {
    if (!acceptAll) return;
    const hint = document.querySelector(".keys-hint");
    acceptAll.hidden = !open;
    if (hint) hint.hidden = !open;
    acceptAll.querySelector("[data-open-count]").textContent = String(open);
    acceptAll.dataset.confirm = `Alle ${open} offenen Vorschläge unverändert übernehmen?`;
  };

  // Verwerfen zurücknehmen: das gemerkte Formular kommt wieder, samt Änderungen.
  const reopen = async (done, form, button) => {
    button.disabled = true;
    try {
      const url = form.action.replace(/\/accept$/, "/reopen");
      const res = await fetch(url, { method: "POST", headers: { Accept: "application/json" } });
      const data = await res.json();
      if (!data.ok || data.status !== "open") throw new Error(String(res.status));
      form.classList.remove("busy");
      done.replaceWith(form);
      updateCount(data.open);
      select(form);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Nicht gespeichert, nochmals";
    }
  };

  const decide = async (form, reject) => {
    const url = reject ? form.querySelector("[formaction]").getAttribute("formaction") : form.action;
    form.querySelector(".fail")?.remove();
    form.classList.add("busy");
    try {
      const res = await fetch(url, {
        method: "POST",
        body: reject ? null : new FormData(form),
        headers: { Accept: "application/json" },
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || String(res.status));
      const next = openForms().find((f) => form.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING);
      const done = document.createElement("div");
      done.className = `suggestion done ${data.status}`;
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = data.status === "accepted" ? "übernommen" : "verworfen";
      const text = document.createElement("span");
      text.className = "math";
      text.textContent = data.text;
      done.append(badge, text);
      if (data.status === "rejected") {
        const undo = document.createElement("button");
        undo.type = "button";
        undo.className = "link";
        undo.textContent = "Rückgängig";
        undo.addEventListener("click", () => reopen(done, form, undo));
        done.append(undo);
      }
      const wasCurrent = form === current;
      form.replaceWith(done);
      renderMath(done);
      updateCount(data.open);
      if (wasCurrent) select(next || openForms()[0]);
    } catch (error) {
      form.classList.remove("busy");
      const fail = document.createElement("span");
      fail.className = "fail";
      fail.textContent = error instanceof SyntaxError || error instanceof TypeError
        ? "Nicht gespeichert (Netz?). Nochmals versuchen."
        : `Nicht gespeichert: ${error.message}`;
      form.querySelector(".actions").append(fail);
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form.suggestion");
    if (!form || event.defaultPrevented) return;
    event.preventDefault();
    decide(form, Boolean(event.submitter && event.submitter.hasAttribute("formaction")));
  });
  document.addEventListener("focusin", (event) => {
    const form = event.target.closest("form.suggestion");
    if (form && form !== current) {
      if (current) current.classList.remove("current");
      current = form;
      form.classList.add("current");
    }
  });
  document.addEventListener("keydown", (event) => {
    // Nur Schreibfelder sperren die Tasten, nicht Ankreuzfelder und Optionsknöpfe.
    const field = event.target.closest('input:not([type="checkbox"]):not([type="radio"]), textarea, select');
    if (field) {
      const form = field.closest("form.suggestion");
      if (!form) return;
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        if (form.reportValidity()) decide(form, false);
      } else if (event.key === "Escape") {
        field.blur();
      }
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key === "j") step(1);
    else if (key === "k") step(-1);
    else if (["a", "v", "e"].includes(key)) {
      if (!current) {
        step(0);
        return;
      }
      if (key === "a" && current.reportValidity()) decide(current, false);
      else if (key === "v") decide(current, true);
      else if (key === "e") current.querySelector('textarea[name="front"]').focus();
    } else return;
    event.preventDefault();
  });
}

// Farbwähler in der App-Leiste: Punkt auf dem Verlauf setzen oder ziehen,
// Vorschau sofort, gespeichert beim Loslassen (pro Konto).
function setupTheme(menu) {
  const theme = window.studyTheme;
  if (!theme) return;
  const SATURATION = 65;
  const L_TOP = 92;
  const L_BOTTOM = 8;
  const field = menu.querySelector("[data-accent-field]");
  const bar = menu.querySelector("[data-bg-field]");
  const status = menu.querySelector("[data-theme-status]");
  const resets = Object.fromEntries([...menu.querySelectorAll("[data-reset]")].map((b) => [b.dataset.reset, b]));
  let { accent, background } = theme.current;
  let timer = null;
  const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // Wo die Punkte stehen: gewählte Werte, sonst die Farben, die gerade gelten.
  const place = () => {
    const [h, , l] = theme.hexToHsl(accent || css("--accent"));
    const accentHandle = field.querySelector(".handle");
    accentHandle.style.left = `${(h / 360) * 100}%`;
    accentHandle.style.top = `${clamp((L_TOP - l) / (L_TOP - L_BOTTOM)) * 100}%`;
    accentHandle.style.background = accent || css("--accent");
    field.setAttribute("aria-valuetext", `Farbton ${Math.round(h)} Grad, Helligkeit ${Math.round(l)} Prozent`);
    const level = background ?? Math.round((theme.hexToHsl(css("--bg"))[2]));
    const bgHandle = bar.querySelector(".handle");
    bgHandle.style.left = `${100 - level}%`;
    bgHandle.style.background = theme.gray(level);
    bar.setAttribute("aria-valuenow", String(level));
    bar.setAttribute("aria-valuetext", `${level} Prozent hell`);
    resets.accent.disabled = !accent;
    resets.background.disabled = background == null;
  };
  const update = () => {
    theme.apply(accent, background);
    place();
  };
  const save = async () => {
    clearTimeout(timer);
    const body = new FormData();
    body.set("accent", accent || "");
    body.set("background", background == null ? "" : String(background));
    try {
      const res = await fetch("/settings/theme", { method: "POST", body, headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(String(res.status));
      status.textContent = "Gespeichert.";
    } catch (error) {
      status.textContent = "Nicht gespeichert (Netz?). Nochmals versuchen.";
    }
  };
  const saveSoon = () => {
    clearTimeout(timer);
    timer = setTimeout(save, 500);
  };

  const setAccent = (hue, light) => {
    accent = theme.hslToHex(((hue % 360) + 360) % 360, SATURATION, clamp(light, L_BOTTOM, L_TOP));
    update();
  };
  const setBackground = (level) => {
    background = Math.round(clamp(level, 0, 100));
    update();
  };

  const drag = (el, onMove) => {
    el.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      el.focus({ preventScroll: true });
      el.setPointerCapture(event.pointerId);
      const move = (ev) => {
        const r = el.getBoundingClientRect();
        onMove(clamp((ev.clientX - r.left) / r.width), clamp((ev.clientY - r.top) / r.height));
      };
      const up = () => {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        el.removeEventListener("pointercancel", up);
        save();
      };
      move(event);
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    });
  };
  drag(field, (x, y) => setAccent(x * 360, L_TOP - y * (L_TOP - L_BOTTOM)));
  drag(bar, (x) => setBackground(100 * (1 - x)));

  field.addEventListener("keydown", (event) => {
    const [h, , l] = theme.hexToHsl(accent || css("--accent"));
    const big = event.shiftKey;
    const moves = {
      ArrowLeft: [-(big ? 30 : 5), 0], ArrowRight: [big ? 30 : 5, 0],
      ArrowUp: [0, big ? 10 : 2], ArrowDown: [0, -(big ? 10 : 2)],
    };
    if (!moves[event.key]) return;
    event.preventDefault();
    setAccent(h + moves[event.key][0], l + moves[event.key][1]);
    saveSoon();
  });
  bar.addEventListener("keydown", (event) => {
    const level = background ?? Number(bar.getAttribute("aria-valuenow"));
    const step = event.shiftKey ? 10 : 1;
    const targets = {
      ArrowLeft: level + step, ArrowUp: level + step, ArrowRight: level - step, ArrowDown: level - step,
      PageUp: level + 10, PageDown: level - 10, Home: 100, End: 0,
    };
    if (!(event.key in targets)) return;
    event.preventDefault();
    setBackground(targets[event.key]);
    saveSoon();
  });
  resets.accent.addEventListener("click", () => {
    accent = null;
    update();
    save();
  });
  resets.background.addEventListener("click", () => {
    background = null;
    update();
    save();
  });

  // Schliessen mit Esc oder Klick daneben.
  menu.addEventListener("toggle", () => {
    if (menu.open) {
      status.textContent = "";
      place();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  // Tasten im Farbwähler sollen nicht die Lern- oder Vorschlagstasten auslösen.
  menu.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  });
}

// Kartentypen vor dem Erzeugen: mindestens einer bleibt angewählt.
function setupTypePills(group) {
  group.addEventListener("change", (event) => {
    if (!group.querySelector('input[type="checkbox"]:checked')) event.target.checked = true;
  });
}

// Vorlagen für die Kurs-Anweisungen: ins leere Feld einsetzen, sonst anhängen.
document.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-template]");
  if (!chip) return;
  const field = chip.closest("form").querySelector("textarea");
  const current = field.value.trim();
  field.value = current ? `${current}\n${chip.dataset.template}` : chip.dataset.template;
  field.focus();
});

// Upload: Datei wählen oder in die Fläche ziehen lädt direkt hoch.
function setupDropzone(form) {
  const input = form.querySelector('input[type="file"]');
  const label = form.querySelector("label strong");
  const send = () => {
    if (!input.files.length) return;
    label.textContent = `«${input.files[0].name}» wird hochgeladen …`;
    form.classList.add("busy");
    form.submit();
  };
  input.addEventListener("change", send);
  ["dragenter", "dragover"].forEach((type) =>
    form.addEventListener(type, (event) => {
      event.preventDefault();
      form.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((type) => form.addEventListener(type, () => form.classList.remove("drag")));
  form.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      send();
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-dropzone]").forEach(setupDropzone);
  renderMath(document);
  const poll = document.querySelector("[data-poll]");
  if (poll) pollDocument(poll);
  const card = document.querySelector("[data-learn]");
  if (card) setupLearn(card);
  const undo = document.querySelector("[data-undo]");
  if (undo) setupUndo(undo);
  if (document.querySelector("form.suggestion")) setupSuggestions();
  document.querySelectorAll("[data-type-pills]").forEach(setupTypePills);
  const themeMenu = document.querySelector("[data-theme-menu]");
  if (themeMenu) setupTheme(themeMenu);
  const wait = document.querySelector("[data-wait]");
  if (wait) setTimeout(() => window.location.reload(), Number(wait.dataset.wait) * 1000 + 1000);
});
