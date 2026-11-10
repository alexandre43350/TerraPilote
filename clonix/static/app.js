"use strict";

/* ------------------------------------------------------------------ outils */
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "icon") el.innerHTML = ICONS[v]; // icônes SVG statiques uniquement
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const icon = (name, cls) => h("i", { class: cls || "", "data-icon": name, icon: name });

const P = (d) => `<svg viewBox="0 0 24 24">${d}</svg>`;
const ICONS = {
  capture: P('<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>'),
  deploy: P('<path d="M12 21V9M7 14l5-5 5 5"/><path d="M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/>'),
  clone: P('<rect x="3" y="3" width="13" height="13" rx="3"/><path d="M8 21h10a3 3 0 0 0 3-3V8"/>'),
  send: P('<circle cx="12" cy="12" r="2"/><path d="M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M5.6 18.4a9 9 0 0 1 0-12.8M18.4 5.6a9 9 0 0 1 0 12.8"/>'),
  recv: P('<path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1.2"/><path d="M2 9a14 14 0 0 1 20 0"/>'),
  verify: P('<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>'),
  disk: P('<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15h.01M11 15h6"/>'),
  usb: P('<rect x="7" y="9" width="10" height="12" rx="2"/><path d="M9 9V3h6v6M10.5 5.5h.01M13.5 5.5h.01"/>'),
  image: P('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'),
  warn: P('<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5h.01"/>'),
  arrow: P('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  home: P('<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>'),
  reboot: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
  power: P('<path d="M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0"/>'),
  close: P('<path d="M6 6l12 12M18 6L6 18"/>'),
  net: P('<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="3" y="16" width="6" height="5" rx="1"/><rect x="15" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M6 16v-2h12v2"/>'),
  cloud: P('<path d="M7 18a4.5 4.5 0 0 1-.5-9A6 6 0 0 1 18 9.5a4 4 0 0 1-1 8.5z"/>'),
  check: P('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  stack: P('<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>'),
  key: P('<circle cx="8" cy="8" r="4.5"/><path d="M11.2 11.2L20 20M17 17l2-2M14 14l2.5-2.5"/>'),
  wrench: P('<path d="M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-2.5-2.5 1.2-1.2a4 4 0 0 1-5-5L3 9.6 5.6 7l2.7 2.7a4 4 0 0 1 5-5z"/>'),
  win: P('<path d="M3 5l8-1v7H3zM13 3.7L21 3v8h-8zM3 13h8v6l-8-1zM13 13h8v8l-8-1z"/>'),
  tux: P('<path d="M12 3a3.5 3.5 0 0 1 3.5 3.5v3c2 1.5 3.5 4 3.5 7 0 1.5-3 3.5-7 3.5s-7-2-7-3.5c0-3 1.5-5.5 3.5-7v-3A3.5 3.5 0 0 1 12 3z"/><path d="M10 7.5h.01M14 7.5h.01"/>'),
};

function size(n) {
  if (n === null || n === undefined) return "—";
  const u = ["o", "Ko", "Mo", "Go", "To"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return (i === 0 ? n.toFixed(0) : n >= 100 ? n.toFixed(0) : n.toFixed(1)).replace(".", ",") + " " + u[i];
}
const rate = (bps) => (bps ? size(bps) + "/s" : "—");
function duration(s) {
  if (s === null || s === undefined || !isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh} h ${String(mm).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
}
function fsColor(fs, mode) {
  if (mode === "dd" || !fs) return "var(--fs-raw)";
  if (fs === "ntfs") return "var(--fs-ntfs)";
  if (fs === "vfat" || fs === "exfat") return "var(--fs-vfat)";
  if (fs.startsWith("ext") || fs === "btrfs" || fs === "xfs") return "var(--fs-ext)";
  if (fs === "swap") return "var(--fs-swap)";
  return "var(--fs-other)";
}
function fsName(fs) {
  return { vfat: "FAT32", ntfs: "NTFS", swap: "swap", BitLocker: "BitLocker", iso9660: "ISO" }[fs] || fs || "brute";
}
const today = () => new Date().toISOString().slice(0, 10);

document.querySelectorAll("i[data-icon]").forEach((el) => (el.innerHTML = ICONS[el.dataset.icon] || ""));

/* --------------------------------------------------------------------- API */
let code = document.querySelector('meta[name="clonix-code"]').content;
if (!code || code === "__CLONIX_CODE__") code = sessionStorage.getItem("clonix-code") || "";

async function api(path, body) {
  const res = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-Clonix-Code": code },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { showLogin(); throw new Error(data.error || "Code d'accès requis"); }
  if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4200);
}

/* ------------------------------------------------------------------- état */
let S = null;
let firstLoad = true;
let dismissedJob = null;

async function refresh() {
  try {
    S = await api("state");
    if (firstLoad && S.job && S.job.state !== "running") dismissedJob = S.job.id;
    firstLoad = false;
    $("#login").hidden = true;
    render();
  } catch (e) {
    if ($("#login").hidden) toast(e.message, true);
  }
}

function loop() {
  const running = S && S.job && S.job.state === "running";
  setTimeout(async () => { await refresh(); loop(); }, running ? 700 : 2500);
}

function tickClock() {
  const d = new Date();
  const txt = d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" }) +
    " · " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  $("#clock").textContent = txt;
}

/* ----------------------------------------------------------------- rendu */
const ACTIONS = [
  { id: "capture", c: "", title: "Capturer", text: "Sauvegarder un disque complet dans une image compressée", run: () => wizardCapture() },
  { id: "deploy", c: "c-blue", title: "Déployer", text: "Restaurer une image sur ce poste en quelques minutes", run: () => wizardDeploy() },
  { id: "clone", c: "c-violet", title: "Cloner", text: "Copier un disque sur un autre, directement", run: () => wizardClone() },
  { id: "send", c: "c-amber", title: "Diffuser", text: "Une image vers plusieurs PC", run: () => wizardSend(), small: true },
  { id: "recv", c: "c-cyan", title: "Recevoir", text: "Image depuis le réseau", run: () => wizardReceive(), small: true },
  { id: "verify", c: "", title: "Vérifier", text: "Intégrité SHA-256", run: () => wizardVerify(), small: true },
  { id: "key", c: "c-amber", title: "Mot de passe", text: "Réinitialiser l'admin d'un poste", run: () => wizardPassword(), small: true },
  { id: "wrench", c: "c-blue", title: "Réparer Windows", text: "Écran bleu au démarrage (0x7B)", run: () => wizardWinfix(), small: true },
];

function renderActions() {
  const box = $("#actions");
  if (box.childElementCount) return;
  for (const a of ACTIONS) {
    box.append(h("button", { class: `action ${a.c} ${a.small ? "small" : ""}`, onclick: a.run },
      icon(a.id, "ico"),
      a.small ? h("div", {}, h("h3", {}, a.title), h("p", {}, a.text)) : [h("h3", {}, a.title), h("p", {}, a.text)],
      icon("arrow", "go")));
  }
}

function stat(label, value, sub, ic) {
  return h("div", { class: "stat" }, icon(ic), h("small", {}, label), h("strong", {}, value), h("span", {}, sub));
}

function render() {
  renderActions();
  $("#version").textContent = `version ${S.version}`;
  $("#chip-demo").hidden = !S.demo;
  const rem = $("#chip-remote");
  rem.hidden = !S.remote;
  if (S.remote) {
    const addr = (S.remote.addresses || [])[0];
    rem.textContent = `Accès distant · ${addr ? addr + ":" + S.remote.port : "port " + S.remote.port} · code ${S.remote.code}`;
  }

  const st = S.storage;
  const capacity = S.disks.reduce((a, d) => a + d.size, 0);
  const imgTotal = S.images.reduce((a, i) => a + i.size, 0);
  $("#stats").replaceChildren(
    stat("Disques détectés", S.disks.length, `${size(capacity)} au total`, "disk"),
    stat("Images", st.mounted ? S.images.length : "—", st.mounted ? `${size(imgTotal)} stockés` : "dépôt non monté", "stack"),
    stat("Espace libre du dépôt", st.mounted ? size(st.free) : "—", st.mounted ? st.fstype.toUpperCase() || "local" : "à configurer", "cloud"),
    stat("Réseau", S.interface || "aucun", S.remote && S.remote.addresses.length ? S.remote.addresses[0] : (S.interface ? "connecté" : "câble débranché ?"), "net"),
  );

  const side = $("#side-storage");
  const used = st.mounted && st.free !== null ? Math.min(1, imgTotal / (imgTotal + st.free)) : 0;
  side.replaceChildren(
    h("div", { class: "label" }, "Dépôt d'images"),
    h("div", { class: "title" }, h("span", { class: "dot" + (st.mounted ? " ok" : "") }), st.mounted ? (st.source || st.dir) : "Non monté"),
    h("div", { class: "sub" }, st.mounted ? `${size(st.free)} libres · ${st.fstype || "local"}` : "Choisissez où lire et écrire les images"),
    st.mounted ? h("div", { class: "meter" }, h("div", { style: `width:${Math.max(used * 100, 2)}%` })) : null,
    h("button", { class: "btn" + (st.mounted ? "" : " primary"), onclick: () => storageDialog() }, st.mounted ? "Changer de dépôt" : "Configurer le dépôt"));
  $("#side-net").replaceChildren(icon("net"), S.interface ? `Interface ${S.interface}` : "Aucun réseau");

  $("#disk-count").textContent = `${S.disks.length} disque(s)`;
  $("#disks").replaceChildren(...S.disks.map((d) => diskCard(d)));
  $("#image-count").textContent = st.mounted ? `${S.images.length} image(s)` : "—";
  $("#images").replaceChildren(...imageListOrEmpty());
  renderJob();
}

function imageListOrEmpty() {
  if (!S.storage.mounted) {
    return [h("div", { class: "empty" }, "Aucun dépôt d'images monté.", h("br"),
      h("button", { class: "btn primary", onclick: () => storageDialog() }, "Configurer le dépôt"))];
  }
  if (!S.images.length) return [h("div", { class: "empty" }, "Le dépôt ne contient encore aucune image.")];
  return S.images.map((i) => imageCard(i));
}

function partBar(parts, total) {
  const used = parts.reduce((a, p) => a + p.size, 0);
  const segs = parts.map((p) => h("span", {
    style: `flex:${Math.max(p.size / total, 0.004)};background:${fsColor(p.fstype, p.mode)}`,
    title: `${p.path || "Partition " + p.number} · ${fsName(p.fstype)} · ${size(p.size)}`,
  }));
  if (total - used > total * 0.01) segs.push(h("span", { style: `flex:${(total - used) / total};background:transparent` }));
  return h("div", { class: "pbar" }, segs);
}

function legend(parts) {
  return h("div", { class: "legend" }, parts.map((p) =>
    h("span", {}, h("i", { style: `background:${fsColor(p.fstype, p.mode)}` }), `${p.label || fsName(p.fstype)} ${size(p.size)}`)));
}

function diskBadges(d) {
  const b = [];
  if (d.protected) {
    const why = d.protected.replace(/^(la|le) /, "");
    b.push(h("span", { class: "badge lock" }, "🔒 " + why.charAt(0).toUpperCase() + why.slice(1)));
  } else if (d.mounted) b.push(h("span", { class: "badge lock" }, "monté"));
  if (d.readonly) b.push(h("span", { class: "badge" }, "lecture seule"));
  return b;
}

function diskCard(d, opts = {}) {
  return h("div", {
    class: "disk" + (opts.pick ? " pick" : "") + (opts.disabled ? " disabled" : "") + (opts.selected ? " selected" : ""),
    onclick: opts.disabled ? null : opts.onclick,
  },
  h("div", { class: "row-top" },
    icon(d.transport === "usb" ? "usb" : "disk", "tile-ico"),
    h("div", { class: "meta" }, h("strong", {}, d.path),
      h("div", { class: "sub" }, [d.model, (d.transport || "").toUpperCase()].filter(Boolean).join(" · "))),
    ...diskBadges(d),
    opts.reason ? h("span", { class: "badge bad" }, opts.reason) : null,
    h("div", { class: "size" }, size(d.size))),
  d.partitions.length ? partBar(d.partitions, d.size) : h("div", { class: "pbar" }),
  d.partitions.length ? legend(d.partitions) : h("div", { class: "legend" }, "Aucune partition — disque vierge"));
}

function imageCard(i, opts = {}) {
  return h("div", { class: "img" + (opts.pick ? " pick" : "") + (opts.selected ? " selected" : ""), onclick: opts.onclick },
    h("div", { class: "row-top" },
      icon("image", "tile-ico"),
      h("div", { class: "meta" }, h("strong", {}, i.name),
        h("div", { class: "sub" }, [i.description, (i.created || "").slice(0, 10)].filter(Boolean).join(" · "))),
      h("div", { class: "size" }, size(i.size))),
    partBar(i.partitions, i.source.size || 1),
    h("div", { class: "legend" }, `Disque source ${size(i.source.size)}${i.source.model ? " · " + i.source.model : ""} · ${i.partitions.length} partition(s)`));
}

/* ----------------------------------------------------------------- modale */
function openModal(...content) {
  $("#modal-body").replaceChildren(...content.flat().filter((c) => c !== null && c !== undefined && c !== false));
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }
$("#modal-close").onclick = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function wizHead(title, sub, step, steps) {
  return h("div", { class: "wiz-head" }, h("h2", {}, title), h("p", { class: "muted" }, sub),
    steps > 1 ? h("div", { class: "steps" }, Array.from({ length: steps }, (_, i) => h("span", { class: i <= step ? "on" : "" }))) : null);
}

function wizFoot(back, next) {
  return h("div", { class: "wiz-foot" },
    back ? h("button", { class: "btn", onclick: back.fn }, back.label || "Retour") : h("span"),
    next ? h("button", { class: "btn " + (next.cls || "primary"), onclick: next.fn, disabled: next.disabled }, next.label) : null);
}

function needStorage() {
  if (S.storage.mounted) return false;
  openModal(wizHead("Dépôt d'images requis", "Indiquez d'abord où lire ou enregistrer les images."),
    wizFoot(null, { label: "Configurer le dépôt", fn: () => storageDialog() }));
  return true;
}

function needImages() {
  if (needStorage()) return true;
  if (S.images.length) return false;
  openModal(wizHead("Aucune image", "Le dépôt ne contient aucune image. Commencez par capturer un disque."),
    wizFoot(null, { label: "Capturer un disque", fn: () => wizardCapture() }));
  return true;
}

function pickDisks(list, selected, onPick, check) {
  return h("div", { class: "pick-list" }, list.map((d) => {
    const reason = check ? check(d) : null;
    return diskCard(d, { pick: true, disabled: !!reason, reason, selected: selected === d.path, onclick: () => onPick(d) });
  }));
}

function targetProblem(d, need, exclude) {
  if (d.path === exclude) return "source";
  if (d.protected) return "protégé";
  if (d.mounted) return "monté";
  if (d.readonly) return "lecture seule";
  if (need && d.size < need) return "trop petit";
  return null;
}

const flow = () => icon("arrow", "flow");

function dangerConfirm(d, onValid, opts = {}) {
  const input = h("input", { placeholder: d.name, autocomplete: "off", spellcheck: "false" });
  const zone = h("div", { class: "danger-zone" },
    h("h3", {}, icon("warn"), opts.title || "Effacement total du disque"),
    h("p", { class: "note" }, opts.text || `Toutes les données de ${d.path} (${d.model || "disque"}, ${size(d.size)}) seront définitivement remplacées.`),
    h("div", { class: "field", style: "margin:14px 0 0" },
      h("label", {}, "Tapez ", h("code", {}, d.name), " pour confirmer"), input));
  input.addEventListener("input", () => onValid(input.value.trim() === d.name, input.value.trim()));
  setTimeout(() => input.focus(), 60);
  return zone;
}

async function launch(action, body) {
  try {
    const job = await api(action, body);
    closeModal();
    dismissedJob = null;
    S.job = job;
    renderJob();
    refresh();
  } catch (e) { toast(e.message, true); }
}

/* --------------------------------------------------------------- assistants */
function wizardCapture(step = 0, sel = {}) {
  if (needStorage()) return;
  if (step === 0) {
    return openModal(wizHead("Capturer un disque", "Choisissez le disque à sauvegarder. Il ne sera pas modifié.", 0, 2),
      pickDisks(S.disks.filter((d) => !d.protected), sel.disk, (d) => wizardCapture(0, { ...sel, disk: d.path }),
        (d) => (d.mounted ? "monté" : d.partitions.length ? null : "vide")),
      wizFoot(null, { label: "Continuer", disabled: !sel.disk, fn: () => wizardCapture(1, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  const name = h("input", { value: sel.name || `${d.name}-${today()}` });
  const desc = h("input", { value: sel.description || "", placeholder: "ex. Windows 11 + Office, poste accueil" });
  const hasNtfs = d.partitions.some((p) => p.fstype === "ntfs");
  const slimBox = h("input", { type: "checkbox" });
  slimBox.checked = sel.slim === true;
  let level = sel.level || "auto";
  const seg = h("div", { class: "seg" }, [["auto", "Auto (recommandé)"], ["1", "Ultra-rapide"], ["6", "Équilibré"], ["15", "Compact"]].map(([v, l]) =>
    h("button", { class: v === level ? "on" : "", onclick: (e) => { level = v; seg.querySelectorAll("button").forEach((b) => b.classList.remove("on")); e.currentTarget.classList.add("on"); } }, l)));
  openModal(wizHead("Capturer un disque", "Nommez l'image et lancez la capture.", 1, 2),
    diskCard(d),
    h("div", { style: "height:18px" }),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("label", {}, "Nom de l'image"), name),
      h("div", { class: "field" }, h("label", {}, "Description"), desc)),
    h("div", { class: "field" }, h("label", {}, "Compression"), seg,
      h("p", { class: "note" }, "Auto : compression la plus rapide sur un disque local, adaptée au débit sur un partage réseau. Seuls les blocs utilisés sont copiés.")),
    hasNtfs ? h("label", { class: "check" }, slimBox, h("span", {},
      h("strong", {}, "Alléger l'image (modifie le PC source). "),
      "Retire le fichier de veille prolongée et le fichier d'échange de Windows (souvent 5 à 20 Go), que Windows recrée tout seul. Capture plus rapide et image plus petite, comme AOMEI ou Acronis.")) : null,
    wizFoot({ fn: () => wizardCapture(0, { ...sel, name: name.value, description: desc.value, level }) },
      { label: "Lancer la capture", fn: () => launch("capture", { disk: d.path, name: name.value.trim(), description: desc.value, level, slim: hasNtfs && slimBox.checked }) }));
  name.focus();
}

function imagePicker(selected, onPick) {
  return h("div", { class: "pick-list" }, S.images.map((i) => imageCard(i, { pick: true, selected: selected === i.name, onclick: () => onPick(i) })));
}

function wizardDeploy(step = 0, sel = {}) {
  if (needImages()) return;
  if (step === 0) {
    return openModal(wizHead("Déployer une image", "Choisissez l'image à restaurer.", 0, 3),
      imagePicker(sel.image, (i) => wizardDeploy(0, { ...sel, image: i.name })),
      wizFoot(null, { label: "Continuer", disabled: !sel.image, fn: () => wizardDeploy(1, sel) }));
  }
  const img = S.images.find((i) => i.name === sel.image);
  if (step === 1) {
    return openModal(wizHead("Déployer une image", `« ${img.name} » nécessite ${size(img.required)}. Choisissez le disque cible.`, 1, 3),
      pickDisks(S.disks, sel.disk, (d) => wizardDeploy(1, { ...sel, disk: d.path }), (d) => targetProblem(d, img.required)),
      wizFoot({ fn: () => wizardDeploy(0, sel) }, { label: "Continuer", disabled: !sel.disk, fn: () => wizardDeploy(2, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  let typed = "";
  const uu = h("input", { type: "checkbox" });
  const adapt = h("input", { type: "checkbox" });
  adapt.checked = true;
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("deploy", { image: img.name, disk: d.path, confirm: typed, new_uuids: uu.checked, adapt: adapt.checked }) }, "Effacer et déployer");
  openModal(wizHead("Déployer une image", "Dernière vérification avant l'écriture.", 2, 3),
    h("div", { class: "arrow" }, imageCard(img), flow(), diskCard(d)),
    h("div", { style: "height:18px" }),
    dangerConfirm(d, (valid, v) => { typed = v; go.disabled = !valid; }),
    h("label", { class: "check" }, adapt, h("span", {}, "Adapter Windows à ce PC (recommandé). Évite l'écran bleu INACCESSIBLE_BOOT_DEVICE quand l'image vient d'un autre PC : Clonix active les pilotes de disque standard de Windows. Seule la copie déployée est modifiée.")),
    h("label", { class: "check" }, uu, h("span", {}, "Générer de nouveaux identifiants de disque et de partitions (déconseillé pour Windows).")),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardDeploy(1, sel) }, "Retour"), go));
}

function wizardClone(step = 0, sel = {}) {
  if (step === 0) {
    return openModal(wizHead("Cloner un disque", "Choisissez le disque SOURCE (il ne sera pas modifié).", 0, 3),
      pickDisks(S.disks.filter((d) => !d.protected), sel.source, (d) => wizardClone(0, { ...sel, source: d.path }),
        (d) => (d.mounted ? "monté" : d.partitions.length ? null : "vide")),
      wizFoot(null, { label: "Continuer", disabled: !sel.source, fn: () => wizardClone(1, sel) }));
  }
  const src = S.disks.find((x) => x.path === sel.source);
  if (step === 1) {
    return openModal(wizHead("Cloner un disque", `Source : ${src.path}. Choisissez le disque CIBLE.`, 1, 3),
      pickDisks(S.disks, sel.target, (d) => wizardClone(1, { ...sel, target: d.path }), (d) => targetProblem(d, null, src.path)),
      wizFoot({ fn: () => wizardClone(0, sel) }, { label: "Continuer", disabled: !sel.target, fn: () => wizardClone(2, sel) }));
  }
  const dst = S.disks.find((x) => x.path === sel.target);
  let typed = "";
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("clone", { source: src.path, target: dst.path, confirm: typed }) }, "Effacer et cloner");
  openModal(wizHead("Cloner un disque", "Dernière vérification avant l'écriture.", 2, 3),
    h("div", { class: "arrow" }, diskCard(src), flow(), diskCard(dst)),
    h("div", { style: "height:18px" }),
    dangerConfirm(dst, (valid, v) => { typed = v; go.disabled = !valid; }),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardClone(1, sel) }, "Retour"), go));
}

function wizardSend(step = 0, sel = {}) {
  if (needImages()) return;
  if (step === 0) {
    return openModal(wizHead("Diffuser en multicast", "Choisissez l'image à envoyer à plusieurs PC en même temps.", 0, 2),
      imagePicker(sel.image, (i) => wizardSend(0, { ...sel, image: i.name })),
      wizFoot(null, { label: "Continuer", disabled: !sel.image, fn: () => wizardSend(1, sel) }));
  }
  const n = h("input", { value: sel.receivers || 5, inputmode: "numeric" });
  const bump = (k) => { n.value = Math.max(1, (parseInt(n.value, 10) || 1) + k); };
  const wait = h("select", {}, [["120", "2 minutes"], ["300", "5 minutes"], ["600", "10 minutes"], ["1800", "30 minutes"]].map(([v, l]) => h("option", { value: v, selected: v === "600" }, l)));
  openModal(wizHead("Diffuser en multicast", `Image « ${sel.image} »`, 1, 2),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("label", {}, "Nombre de PC récepteurs"),
        h("div", { class: "stepper" }, h("button", { onclick: () => bump(-1) }, "−"), n, h("button", { onclick: () => bump(1) }, "+"))),
      h("div", { class: "field" }, h("label", {}, "Démarrer au plus tard après"), wait)),
    h("p", { class: "note" }, "Démarrez les PC à équiper sur leur clé Clonix et choisissez « Recevoir ». La diffusion commence dès que tous sont connectés : une seule transmission pour tous les postes."),
    wizFoot({ fn: () => wizardSend(0, sel) }, { label: "Lancer la diffusion", fn: () => launch("mcast-send", { image: sel.image, receivers: parseInt(n.value, 10) || 1, max_wait: parseInt(wait.value, 10) }) }));
}

function wizardReceive(step = 0, sel = {}) {
  if (step === 0) {
    return openModal(wizHead("Recevoir une image multicast", "Choisissez le disque de ce PC qui recevra l'image.", 0, 2),
      pickDisks(S.disks, sel.disk, (d) => wizardReceive(0, { ...sel, disk: d.path }), (d) => targetProblem(d)),
      wizFoot(null, { label: "Continuer", disabled: !sel.disk, fn: () => wizardReceive(1, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  let typed = "";
  const adapt = h("input", { type: "checkbox" });
  adapt.checked = true;
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("mcast-recv", { disk: d.path, confirm: typed, adapt: adapt.checked }) }, "Effacer et attendre l'émetteur");
  openModal(wizHead("Recevoir une image multicast", `Interface réseau : ${S.interface || "aucune"}`, 1, 2),
    diskCard(d), h("div", { style: "height:18px" }),
    dangerConfirm(d, (valid, v) => { typed = v; go.disabled = !valid; }),
    h("label", { class: "check" }, adapt, h("span", {}, "Adapter Windows à ce PC (recommandé). Évite l'écran bleu INACCESSIBLE_BOOT_DEVICE quand l'image vient d'un autre PC : Clonix active les pilotes de disque standard de Windows. Seule la copie déployée est modifiée.")),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardReceive(0, sel) }, "Retour"), go));
}

function wizardWinfix(step = 0, sel = {}) {
  if (step === 0) {
    return openModal(wizHead("Réparer Windows", "Choisissez le disque du Windows qui affiche INACCESSIBLE_BOOT_DEVICE au démarrage.", 0, 2),
      pickDisks(S.disks, sel.disk, (d) => wizardWinfix(0, { disk: d.path }), (d) => targetProblem(d) || (d.partitions.some((p) => p.fstype === "ntfs") ? null : "pas de Windows")),
      wizFoot(null, { label: "Continuer", disabled: !sel.disk, fn: () => wizardWinfix(1, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  let typed = "";
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("winfix", { disk: d.path, confirm: typed }) }, "Réparer le démarrage");
  openModal(wizHead("Réparer Windows", "Clonix active les pilotes de disque standard de Windows (NVMe et SATA), comme l'option « Universal Restore » d'AOMEI ou Acronis.", 1, 2),
    diskCard(d), h("div", { style: "height:18px" }),
    dangerConfirm(d, (valid, v) => { typed = v; go.disabled = !valid; }, {
      title: "Modification du registre de Windows",
      text: `Seul le registre système du Windows de ${d.path} est modifié ; vos fichiers ne sont pas touchés. Si le PC utilise Intel VMD (réglage du BIOS), désactivez VMD en plus.`,
    }),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardWinfix(0, sel) }, "Retour"), go));
}

function wizardVerify(sel = {}) {
  if (needImages()) return;
  openModal(wizHead("Vérifier une image", "Relit chaque fichier et contrôle sa somme SHA-256."),
    imagePicker(sel.image, (i) => wizardVerify({ image: i.name })),
    wizFoot(null, { label: "Vérifier", disabled: !sel.image, fn: () => launch("verify", { image: sel.image }) }));
}

async function wizardPassword(step = 0, sel = {}) {
  if (step === 0) {
    return openModal(wizHead("Réinitialiser le mot de passe", "Choisissez le disque du poste à réinitialiser. Seul le fichier des comptes est modifié, pas vos données.", 0, 2),
      pickDisks(S.disks.filter((d) => !d.protected), sel.disk, (d) => wizardPassword(0, { ...sel, disk: d.path }),
        (d) => (d.mounted ? "monté" : d.partitions.length ? null : "vide")),
      wizFoot(null, { label: "Analyser le disque", disabled: !sel.disk, fn: () => wizardPassword(1, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  if (!sel.info) {
    openModal(wizHead("Réinitialiser le mot de passe", `Analyse de ${d.path}…`, 1, 2),
      h("div", { class: "empty" }, "Montage de la partition système et lecture des comptes…"));
    try {
      const info = await api("accounts", { disk: d.path });
      return wizardPassword(1, { ...sel, info });
    } catch (e) {
      return openModal(wizHead("Réinitialiser le mot de passe", `Disque ${d.path}`, 1, 2),
        h("p", { class: "error-box" }, e.message),
        wizFoot({ fn: () => wizardPassword(0, sel) }, null));
    }
  }
  const info = sel.info;
  const isWin = info.os === "windows";
  let act = isWin ? "clear" : "clear";
  let typed = "";
  const pass = h("input", { type: "password", autocomplete: "new-password", placeholder: "nouveau mot de passe" });
  const passField = h("div", { class: "field", hidden: true }, h("label", {}, "Nouveau mot de passe"), pass);
  const go = h("button", { class: "btn danger", disabled: true }, "Supprimer le mot de passe");

  function sync() {
    go.textContent = act === "set" ? "Changer le mot de passe" : "Supprimer le mot de passe";
    passField.hidden = act !== "set";
    go.disabled = !sel.user || typed !== d.name || (act === "set" && !pass.value);
  }
  pass.addEventListener("input", sync);
  go.onclick = () => launch("password", { disk: d.path, user: sel.user, action: act, password: pass.value, confirm: typed });

  const seg = isWin ? null : h("div", { class: "seg" }, [["clear", "Supprimer le mot de passe"], ["set", "Définir un nouveau"]].map(([v, l]) =>
    h("button", { class: v === act ? "on" : "", onclick: (e) => { act = v; seg.querySelectorAll("button").forEach((b) => b.classList.remove("on")); e.currentTarget.classList.add("on"); sync(); } }, l)));

  const chips = h("div", { class: "pick-list" }, info.accounts.map((a) => {
    const el = h("div", { class: "img pick" + (sel.user === a.name ? " selected" : ""), onclick: () => { sel.user = a.name; chips.querySelectorAll(".pick").forEach((x) => x.classList.remove("selected")); el.classList.add("selected"); sync(); } },
      h("div", { class: "row-top" }, icon(isWin ? "win" : "tux", "tile-ico"),
        h("div", { class: "meta" }, h("strong", {}, a.name), h("div", { class: "sub" }, a.admin ? "Administrateur" : "Utilisateur")),
        a.admin ? h("span", { class: "badge lock" }, "admin") : null));
    return el;
  }));
  openModal(wizHead("Réinitialiser le mot de passe", `${isWin ? "Windows" : "Linux"} détecté sur ${info.partition}. Choisissez le compte.`, 1, 2),
    chips,
    h("div", { style: "height:16px" }),
    seg, seg ? h("div", { style: "height:14px" }) : null, passField,
    isWin ? h("p", { class: "note" }, "Sous Windows, le mot de passe est seulement effacé : vous en choisirez un nouveau au premier démarrage (définir un mot de passe précis n'est pas fiable sous Windows 10/11).") : null,
    dangerConfirm(d, (valid, v) => { typed = v; sync(); }, {
      title: "Modification du poste",
      text: `Seul le fichier des comptes de ${d.path} sera modifié ; vos fichiers et le système ne sont pas touchés. Le compte choisi pourra se connecter différemment.`,
    }),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardPassword(0, { disk: sel.disk }) }, "Retour"), go));
}

function storageDialog(kind) {
  const hasKey = S.storage.clonix_key;
  if (!kind) kind = hasKey ? "clonix" : "smb";
  const tabs = [];
  if (hasKey) tabs.push(["clonix", "Clé de démarrage"]);
  tabs.push(["smb", "Partage Windows / NAS"], ["nfs", "NFS"], ["local", "Disque local"]);
  const seg = h("div", { class: "seg" }, tabs.map(([k, l]) => h("button", { class: k === kind ? "on" : "", onclick: () => storageDialog(k) }, l)));
  let form, submit;
  if (kind === "clonix") {
    form = [h("div", { class: "note", style: "margin:0" },
      "Les images seront enregistrées sur la clé qui a démarré Clonix : dans sa partition « CLONIX-DATA », ou dans la partition Ventoy si vous utilisez Ventoy. Vous pourrez relire la clé depuis Windows pour récupérer vos images.")];
    submit = () => ({ kind: "clonix" });
  } else if (kind === "smb") {
    const share = h("input", { placeholder: "//192.168.1.10/images" });
    const user = h("input", { placeholder: "vide = invité", autocomplete: "off" });
    const pass = h("input", { type: "password", autocomplete: "off" });
    const dom = h("input", { placeholder: "facultatif" });
    form = [h("div", { class: "field" }, h("label", {}, "Partage"), share),
      h("div", { class: "row" }, h("div", { class: "field" }, h("label", {}, "Utilisateur"), user), h("div", { class: "field" }, h("label", {}, "Mot de passe"), pass)),
      h("div", { class: "field" }, h("label", {}, "Domaine"), dom)];
    submit = () => ({ kind, target: share.value.trim(), user: user.value.trim(), password: pass.value, domain: dom.value.trim() });
    setTimeout(() => share.focus(), 60);
  } else if (kind === "nfs") {
    const exp = h("input", { placeholder: "192.168.1.10:/srv/images" });
    form = [h("div", { class: "field" }, h("label", {}, "Export NFS"), exp)];
    submit = () => ({ kind, target: exp.value.trim() });
  } else {
    let chosen = null;
    const parts = [];
    for (const d of S.disks) for (const p of d.partitions) if (p.fstype && !["swap", "iso9660"].includes(p.fstype)) parts.push({ d, p });
    const list = h("div", { class: "pick-list" }, parts.length ? parts.map(({ d, p }) => {
      const el = h("div", { class: "img pick", onclick: () => { chosen = p.path; list.querySelectorAll(".pick").forEach((x) => x.classList.remove("selected")); el.classList.add("selected"); } },
        h("div", { class: "row-top" }, icon("disk", "tile-ico"),
          h("div", { class: "meta" }, h("strong", {}, `${p.path}${p.label ? " · " + p.label : ""}`), h("div", { class: "sub" }, `${fsName(p.fstype)} · ${d.model || d.path}`)),
          h("div", { class: "size" }, size(p.size))));
      return el;
    }) : h("div", { class: "empty" }, "Aucune partition utilisable. Branchez un disque externe."));
    form = [list];
    submit = () => ({ kind, target: chosen });
  }
  const foot = h("div", { class: "wiz-foot" },
    S.storage.mounted ? h("button", { class: "btn danger-ghost", onclick: () => mount({ kind: "umount" }) }, "Démonter le dépôt actuel") : h("span"),
    h("button", { class: "btn primary", onclick: () => mount(submit()) }, "Monter le dépôt"));
  openModal(wizHead("Dépôt d'images", S.storage.mounted ? `Actuellement : ${S.storage.source} (${S.storage.fstype})` : "Où sont stockées vos images ?"),
    seg, h("div", { style: "height:18px" }), ...form, foot);
}

async function mount(body) {
  if (!["umount", "clonix"].includes(body.kind) && !body.target) return toast("Indiquez l'emplacement du dépôt", true);
  try {
    await api("storage", body);
    closeModal();
    toast(body.kind === "umount" ? "Dépôt démonté" : "Dépôt monté");
    refresh();
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ tâche */
const CIRC = 2 * Math.PI * 52;
const KINDS = { capture: "Capture", deploy: "Déploiement", clone: "Clonage", "mcast-send": "Diffusion multicast", "mcast-recv": "Réception multicast", verify: "Vérification", password: "Mot de passe", winfix: "Réparation Windows" };
let shownJob = null, shownLogs = -1, history = [];

function renderSpark(job) {
  const p = job.progress || {};
  if (job.state === "running" && p.rate) history.push(p.rate);
  if (history.length > 120) history.shift();
  const max = Math.max(...history, 1);
  $("#job-peak").textContent = history.length ? `pic ${rate(max)}` : "";
  if (history.length < 2) { $("#spark-line").setAttribute("d", ""); $("#spark-area").setAttribute("d", ""); return; }
  const pts = history.map((v, i) => [i * 300 / (history.length - 1), 66 - (v / max) * 58]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  $("#spark-line").setAttribute("d", line);
  $("#spark-area").setAttribute("d", `${line}L300,70L0,70Z`);
}

function renderPlan(job) {
  const p = job.progress || {};
  const plan = p.plan || [];
  const done = job.state === "done";
  $("#job-plan").replaceChildren(...plan.map((label, i) => {
    const cls = done || i < p.step - 1 ? "done" : i === p.step - 1 ? "now" : "";
    return h("li", { class: cls }, h("b", { icon: cls === "done" ? "check" : null }, cls === "done" ? null : String(i + 1)), label);
  }));
}

function renderJob() {
  const job = S && S.job;
  const view = $("#job");
  if (!job || job.id === dismissedJob) { view.hidden = true; return; }
  if (shownJob !== job.id) { history = []; shownLogs = -1; }
  view.hidden = false;
  view.className = "job " + job.state;
  const p = job.progress || {};
  const done = job.state === "done";
  const pct = done ? 100 : Math.floor((p.overall || 0) * 1000) / 10;
  $("#job-kind").textContent = KINDS[job.kind] || "Opération";
  $("#job-title").textContent = job.title;
  $("#job-pct").textContent = pct.toFixed(pct >= 100 ? 0 : 1).replace(".", ",");
  $("#ring-fg").style.strokeDashoffset = CIRC * (1 - pct / 100);
  const stepPct = p.fraction ? p.fraction * 100 : done ? 100 : 0;
  $("#job-bar").style.width = stepPct + "%";
  $("#job-step-pct").textContent = Math.floor(stepPct) + " %";
  $("#ring-done").hidden = !done;
  document.querySelector(".ring-text").style.visibility = done ? "hidden" : "visible";
  const states = { done: "Terminé avec succès", error: "L'opération a échoué", cancelled: "Opération annulée" };
  $("#job-step").textContent = states[job.state] || (p.label ? `${p.label} · étape ${p.step} sur ${p.steps}` : "Préparation…");
  $("#job-rate").textContent = job.state === "running" ? rate(p.rate) : "—";
  $("#job-eta").textContent = job.state === "running" ? duration(p.eta) : "—";
  $("#job-elapsed").textContent = duration((job.finished || Date.now() / 1000) - job.started);
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";
  $("#job-cancel").hidden = job.state !== "running";
  $("#job-close").hidden = job.state === "running";
  if (job.state === "error") $("#job-log").hidden = false;
  renderSpark(job);
  renderPlan(job);

  const log = $("#job-log");
  const sig = job.logs.length ? job.logs[job.logs.length - 1].t + job.logs.length : 0;
  if (shownJob !== job.id || shownLogs !== sig) {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    log.textContent = job.logs.map((l) => l.msg).join("\n");
    if (atBottom || shownJob !== job.id) log.scrollTop = log.scrollHeight;
    shownLogs = sig;
  }
  shownJob = job.id;
}

$("#job-cancel").onclick = async () => {
  if (!confirm("Annuler l'opération en cours ? Le disque cible restera inutilisable tant qu'une nouvelle copie n'aura pas été faite.")) return;
  try { await api("cancel", {}); } catch (e) { toast(e.message, true); }
};
$("#job-close").onclick = () => { dismissedJob = S.job.id; renderJob(); };
$("#job-logtoggle").onclick = () => { const l = $("#job-log"); l.hidden = !l.hidden; l.scrollTop = l.scrollHeight; };

/* ------------------------------------------------------- navigation, divers */
document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", () => {
  document.querySelectorAll(".nav a").forEach((x) => x.classList.toggle("on", x === a));
}));
document.querySelectorAll("[data-power]").forEach((b) => (b.onclick = async () => {
  const action = b.dataset.power;
  if (!confirm(action === "reboot" ? "Redémarrer le PC ?" : "Éteindre le PC ?")) return;
  try { await api("power", { action }); toast("À bientôt !"); } catch (e) { toast(e.message, true); }
}));

function showLogin() { $("#login").hidden = false; $("#login-code").focus(); }
$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  code = $("#login-code").value.trim();
  sessionStorage.setItem("clonix-code", code);
  try { S = await api("state"); $("#login").hidden = true; render(); }
  catch (err) { $("#login-error").textContent = "Code incorrect"; }
};

tickClock();
setInterval(tickClock, 15000);
if (!code) showLogin();
refresh().then(loop);
