"use strict";

/* ------------------------------------------------------------------ outils */
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // réservé aux icônes SVG statiques
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  capture: '<svg viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
  deploy: '<svg viewBox="0 0 24 24"><path d="M12 21V9M7 14l5-5 5 5"/><path d="M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/></svg>',
  clone: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="13" height="13" rx="3"/><path d="M8 21h10a3 3 0 0 0 3-3V8"/></svg>',
  send: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2"/><path d="M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M5.6 18.4a9 9 0 0 1 0-12.8M18.4 5.6a9 9 0 0 1 0 12.8"/></svg>',
  recv: '<svg viewBox="0 0 24 24"><path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1.2"/><path d="M2 9a14 14 0 0 1 20 0"/></svg>',
  verify: '<svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg>',
  disk: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15h.01M11 15h6"/></svg>',
  usb: '<svg viewBox="0 0 24 24"><rect x="7" y="9" width="10" height="12" rx="2"/><path d="M9 9V3h6v6M10.5 5.5h.01M13.5 5.5h.01"/></svg>',
  image: '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5h.01"/></svg>',
  arrow: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
};

function size(n) {
  if (n === null || n === undefined) return "—";
  const u = ["o", "Ko", "Mo", "Go", "To"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return (i === 0 ? n.toFixed(0) : n >= 100 ? n.toFixed(0) : n.toFixed(1)).replace(".", ",") + " " + u[i];
}
function rate(bps) { return bps ? size(bps) + "/s" : "—"; }
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
  return { vfat: "FAT32 (EFI)", ntfs: "NTFS", swap: "swap", BitLocker: "BitLocker", iso9660: "ISO" }[fs] || fs || "brute";
}
function today() { return new Date().toISOString().slice(0, 10); }

/* --------------------------------------------------------------------- API */
let code = document.querySelector('meta[name="tp-code"]').content;
if (!code || code === "__TP_CODE__") code = sessionStorage.getItem("tp-code") || "";

async function api(path, body) {
  const res = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-TP-Code": code },
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
let shownJob = null;   // id de la tâche affichée
let dismissedJob = null;
let shownLogs = -1;

let firstLoad = true;

async function refresh() {
  try {
    S = await api("state");
    if (firstLoad && S.job && S.job.state !== "running") dismissedJob = S.job.id;
    firstLoad = false;
    $("#login").hidden = true;
    render();
  } catch (e) {
    if (!$("#login").hidden) return;
    toast(e.message, true);
  }
}

function loop() {
  const running = S && S.job && S.job.state === "running";
  setTimeout(async () => { await refresh(); loop(); }, running ? 700 : 2500);
}

/* ----------------------------------------------------------------- rendu */
const ACTIONS = [
  { id: "capture", cls: "", title: "Capturer", text: "Sauvegarder un disque dans une image", run: wizardCapture },
  { id: "deploy", cls: "blue", title: "Déployer", text: "Restaurer une image sur ce PC", run: wizardDeploy },
  { id: "clone", cls: "violet", title: "Cloner", text: "Copier un disque sur un autre", run: wizardClone },
  { id: "send", cls: "amber", title: "Diffuser", text: "Envoyer une image à plusieurs PC", run: wizardSend },
  { id: "recv", cls: "cyan", title: "Recevoir", text: "Recevoir une image du réseau", run: wizardReceive },
  { id: "verify", cls: "", title: "Vérifier", text: "Contrôler l'intégrité d'une image", run: wizardVerify },
];

function renderActions() {
  const box = $("#actions");
  if (box.childElementCount) return;
  for (const a of ACTIONS) {
    box.append(h("button", { class: "action " + a.cls, onclick: () => a.run() },
      h("div", { class: "ico", html: ICONS[a.id] }), h("h3", {}, a.title), h("p", {}, a.text)));
  }
}

function render() {
  renderActions();
  $("#version").textContent = "v" + S.version;
  $("#chip-demo").hidden = !S.demo;
  const st = S.storage;
  const chip = $("#chip-storage");
  chip.replaceChildren(
    h("span", { class: "dot" + (st.mounted ? " ok" : "") }),
    st.mounted ? `Dépôt : ${st.source || st.dir} · ${size(st.free)} libres` : "Dépôt d'images non monté",
  );
  $("#chip-net").textContent = S.interface ? `Réseau : ${S.interface}` : "Réseau : aucun";
  const rem = $("#chip-remote");
  rem.hidden = !S.remote;
  if (S.remote) {
    const addr = (S.remote.addresses || [])[0];
    rem.textContent = `Accès distant : ${addr ? addr + ":" + S.remote.port : "port " + S.remote.port} · code ${S.remote.code}`;
  }

  $("#disk-count").textContent = `${S.disks.length} détecté(s)`;
  $("#disks").replaceChildren(...S.disks.map((d) => diskCard(d)));
  $("#image-count").textContent = st.mounted ? `${S.images.length} image(s)` : "";
  $("#images").replaceChildren(...imageListOrEmpty());
  renderJob();
}

function imageListOrEmpty() {
  if (!S.storage.mounted) {
    return [h("div", { class: "empty" }, "Aucun dépôt d'images monté.",
      h("br"), h("button", { class: "btn primary", onclick: storageDialog }, "Choisir le dépôt"))];
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
    h("span", {}, h("i", { style: `background:${fsColor(p.fstype, p.mode)}` }),
      `${p.label || fsName(p.fstype)} ${size(p.size)}`)));
}

function diskBadges(d) {
  const b = [];
  if (d.protected) {
    const why = d.protected.replace(/^(la|le) /, "");
    b.push(h("span", { class: "badge lock" }, "🔒 " + why.charAt(0).toUpperCase() + why.slice(1)));
  }
  else if (d.mounted) b.push(h("span", { class: "badge lock" }, "monté"));
  if (d.readonly) b.push(h("span", { class: "badge" }, "lecture seule"));
  return b;
}

function diskCard(d, opts = {}) {
  const icon = d.transport === "usb" ? ICONS.usb : ICONS.disk;
  const el = h("div", {
    class: "disk" + (opts.pick ? " pick" : "") + (opts.disabled ? " disabled" : "") + (opts.selected ? " selected" : ""),
    onclick: opts.disabled ? null : opts.onclick,
  },
  h("div", { class: "disk-top" },
    h("div", { class: "disk-ico", html: icon }),
    h("div", { class: "disk-meta" },
      h("div", { class: "disk-name" }, d.path),
      h("div", { class: "disk-sub" }, [d.model, (d.transport || "").toUpperCase()].filter(Boolean).join(" · "))),
    ...diskBadges(d),
    opts.reason ? h("span", { class: "badge bad" }, opts.reason) : null,
    h("div", { class: "disk-size" }, size(d.size))),
  d.partitions.length ? partBar(d.partitions, d.size) : h("div", { class: "pbar" }),
  d.partitions.length ? legend(d.partitions) : h("div", { class: "legend" }, "Aucune partition"));
  return el;
}

function imageCard(i, opts = {}) {
  return h("div", {
    class: "img" + (opts.pick ? " pick" : "") + (opts.selected ? " selected" : ""),
    onclick: opts.onclick,
  },
  h("div", { class: "img-ico", html: ICONS.image }),
  h("div", { class: "img-meta" },
    h("strong", {}, i.name),
    h("div", { class: "disk-sub" },
      [i.description, (i.created || "").slice(0, 10), `source ${size(i.source.size)}`].filter(Boolean).join(" · ")),
    partBar(i.partitions, i.source.size || 1)),
  h("div", { class: "disk-size" }, size(i.size)));
}

/* ----------------------------------------------------------------- modale */
function openModal(...content) {
  $("#modal-body").replaceChildren(...content);
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
    wizFoot(null, { label: "Choisir le dépôt", fn: storageDialog }));
  return true;
}

function needImages() {
  if (needStorage()) return true;
  if (S.images.length) return false;
  openModal(wizHead("Aucune image", "Le dépôt ne contient aucune image. Commencez par capturer un disque."),
    wizFoot(null, { label: "Capturer un disque", fn: wizardCapture }));
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

function dangerConfirm(d, onValid) {
  const input = h("input", { placeholder: d.name, autocomplete: "off", spellcheck: "false" });
  const zone = h("div", { class: "danger-zone" },
    h("h3", { html: ICONS.warn }, "Effacement total du disque"),
    h("p", { class: "note" }, `Toutes les données de ${d.path} (${d.model || "disque"}, ${size(d.size)}) seront définitivement remplacées.`),
    h("div", { class: "field", style: "margin:12px 0 0" },
      h("label", {}, h("span", {}, "Tapez "), h("code", {}, d.name), h("span", {}, " pour confirmer")), input));
  input.addEventListener("input", () => onValid(input.value.trim() === d.name, input.value.trim()));
  setTimeout(() => input.focus(), 50);
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
    const disks = S.disks.filter((d) => !d.protected);
    return openModal(wizHead("Capturer un disque", "Choisissez le disque à sauvegarder.", 0, 2),
      pickDisks(disks, sel.disk, (d) => wizardCapture(0, { ...sel, disk: d.path }), (d) => d.mounted ? "monté" : (d.partitions.length ? null : "vide")),
      wizFoot(null, { label: "Continuer", disabled: !sel.disk, fn: () => wizardCapture(1, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  const name = h("input", { value: sel.name || `${d.name}-${today()}` });
  const desc = h("input", { value: sel.description || "", placeholder: "ex. Windows 11 + Office, poste accueil" });
  let level = sel.level || "auto";
  const seg = h("div", { class: "seg" }, [["auto", "Auto (recommandé)"], ["1", "Ultra-rapide"], ["6", "Équilibré"], ["15", "Compact"]].map(([v, l]) =>
    h("button", { class: v === level ? "on" : "", onclick: (e) => { level = v; seg.querySelectorAll("button").forEach((b) => b.classList.remove("on")); e.target.classList.add("on"); } }, l)));
  openModal(wizHead("Capturer un disque", `Source : ${d.path} · ${d.model || ""} · ${size(d.size)}`, 1, 2),
    diskCard(d),
    h("div", { style: "height:16px" }),
    h("div", { class: "field" }, h("label", {}, "Nom de l'image"), name),
    h("div", { class: "field" }, h("label", {}, "Description"), desc),
    h("div", { class: "field" }, h("label", {}, "Compression"), seg,
      h("p", { class: "note" }, "Auto : compression la plus rapide sur un disque local, adaptée au débit sur un partage réseau.")),
    wizFoot({ fn: () => wizardCapture(0, { ...sel, name: name.value, description: desc.value, level }) },
      { label: "Lancer la capture", fn: () => launch("capture", { disk: d.path, name: name.value.trim(), description: desc.value, level }) }));
  name.focus();
}

function wizardDeploy(step = 0, sel = {}) {
  if (needImages()) return;
  if (step === 0) {
    return openModal(wizHead("Déployer une image", "Choisissez l'image à restaurer.", 0, 3),
      h("div", { class: "pick-list" }, S.images.map((i) => imageCard(i, { pick: true, selected: sel.image === i.name, onclick: () => wizardDeploy(0, { ...sel, image: i.name }) }))),
      wizFoot(null, { label: "Continuer", disabled: !sel.image, fn: () => wizardDeploy(1, sel) }));
  }
  const img = S.images.find((i) => i.name === sel.image);
  if (step === 1) {
    return openModal(wizHead("Déployer une image", `Image « ${img.name} » · ${size(img.required)} nécessaires. Choisissez le disque cible.`, 1, 3),
      pickDisks(S.disks, sel.disk, (d) => wizardDeploy(1, { ...sel, disk: d.path }), (d) => targetProblem(d, img.required)),
      wizFoot({ fn: () => wizardDeploy(0, sel) }, { label: "Continuer", disabled: !sel.disk, fn: () => wizardDeploy(2, sel) }));
  }
  const d = S.disks.find((x) => x.path === sel.disk);
  let ok = false, typed = "";
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("deploy", { image: img.name, disk: d.path, confirm: typed, new_uuids: uu.checked }) }, "Effacer et déployer");
  const uu = h("input", { type: "checkbox" });
  openModal(wizHead("Déployer une image", "Vérifiez avant de lancer.", 2, 3),
    h("div", { class: "arrow" }, imageCard(img), h("div", { html: ICONS.arrow, style: "width:26px;height:26px;color:var(--accent)" }), diskCard(d)),
    h("div", { style: "height:16px" }),
    dangerConfirm(d, (valid, v) => { ok = valid; typed = v; go.disabled = !ok; }),
    h("label", { class: "check" }, uu, h("span", {}, "Générer de nouveaux identifiants de disque et de partitions (déconseillé pour Windows : préférez sysprep).")),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardDeploy(1, sel) }, "Retour"), go));
}

function wizardClone(step = 0, sel = {}) {
  if (step === 0) {
    return openModal(wizHead("Cloner un disque", "Choisissez le disque SOURCE (il ne sera pas modifié).", 0, 3),
      pickDisks(S.disks.filter((d) => !d.protected), sel.source, (d) => wizardClone(0, { ...sel, source: d.path }), (d) => d.mounted ? "monté" : (d.partitions.length ? null : "vide")),
      wizFoot(null, { label: "Continuer", disabled: !sel.source, fn: () => wizardClone(1, sel) }));
  }
  const src = S.disks.find((x) => x.path === sel.source);
  const need = null; // la taille exacte requise est vérifiée par le serveur
  if (step === 1) {
    return openModal(wizHead("Cloner un disque", `Source : ${src.path}. Choisissez le disque CIBLE.`, 1, 3),
      pickDisks(S.disks, sel.target, (d) => wizardClone(1, { ...sel, target: d.path }), (d) => targetProblem(d, need, src.path)),
      wizFoot({ fn: () => wizardClone(0, sel) }, { label: "Continuer", disabled: !sel.target, fn: () => wizardClone(2, sel) }));
  }
  const dst = S.disks.find((x) => x.path === sel.target);
  let typed = "";
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("clone", { source: src.path, target: dst.path, confirm: typed }) }, "Effacer et cloner");
  openModal(wizHead("Cloner un disque", "Vérifiez avant de lancer.", 2, 3),
    h("div", { class: "arrow" }, diskCard(src), h("div", { html: ICONS.arrow, style: "width:26px;height:26px;color:var(--accent)" }), diskCard(dst)),
    h("div", { style: "height:16px" }),
    dangerConfirm(dst, (valid, v) => { typed = v; go.disabled = !valid; }),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardClone(1, sel) }, "Retour"), go));
}

function wizardSend(step = 0, sel = {}) {
  if (needImages()) return;
  if (step === 0) {
    return openModal(wizHead("Diffuser en multicast", "Choisissez l'image à envoyer à plusieurs PC en même temps.", 0, 2),
      h("div", { class: "pick-list" }, S.images.map((i) => imageCard(i, { pick: true, selected: sel.image === i.name, onclick: () => wizardSend(0, { ...sel, image: i.name }) }))),
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
    h("p", { class: "note" }, "Démarrez les PC à équiper sur leur clé TerraPilote et choisissez « Recevoir ». La diffusion commence dès que tous sont connectés : une seule transmission pour tous les postes, au débit du plus lent."),
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
  const go = h("button", { class: "btn danger", disabled: true, onclick: () => launch("mcast-recv", { disk: d.path, confirm: typed }) }, "Effacer et attendre l'émetteur");
  openModal(wizHead("Recevoir une image multicast", `Interface réseau : ${S.interface || "aucune"}`, 1, 2),
    diskCard(d), h("div", { style: "height:16px" }),
    dangerConfirm(d, (valid, v) => { typed = v; go.disabled = !valid; }),
    h("div", { class: "wiz-foot" }, h("button", { class: "btn", onclick: () => wizardReceive(0, sel) }, "Retour"), go));
}

function wizardVerify(sel = {}) {
  if (needImages()) return;
  openModal(wizHead("Vérifier une image", "Relit chaque fichier et contrôle sa somme SHA-256."),
    h("div", { class: "pick-list" }, S.images.map((i) => imageCard(i, { pick: true, selected: sel.image === i.name, onclick: () => wizardVerify({ image: i.name }) }))),
    wizFoot(null, { label: "Vérifier", disabled: !sel.image, fn: () => launch("verify", { image: sel.image }) }));
}

function storageDialog(kind = "smb") {
  const tabs = [["smb", "Partage Windows / NAS"], ["nfs", "NFS"], ["local", "Disque local"]];
  const seg = h("div", { class: "seg" }, tabs.map(([k, l]) => h("button", { class: k === kind ? "on" : "", onclick: () => storageDialog(k) }, l)));
  let form, submit;
  if (kind === "smb") {
    const share = h("input", { placeholder: "//192.168.1.10/images" });
    const user = h("input", { placeholder: "vide = invité", autocomplete: "off" });
    const pass = h("input", { type: "password", autocomplete: "off" });
    const dom = h("input", { placeholder: "facultatif" });
    form = [h("div", { class: "field" }, h("label", {}, "Partage"), share),
      h("div", { class: "row" }, h("div", { class: "field" }, h("label", {}, "Utilisateur"), user), h("div", { class: "field" }, h("label", {}, "Mot de passe"), pass)),
      h("div", { class: "field" }, h("label", {}, "Domaine"), dom)];
    submit = () => ({ kind, target: share.value.trim(), user: user.value.trim(), password: pass.value, domain: dom.value.trim() });
    setTimeout(() => share.focus(), 50);
  } else if (kind === "nfs") {
    const exp = h("input", { placeholder: "192.168.1.10:/srv/images" });
    form = [h("div", { class: "field" }, h("label", {}, "Export NFS"), exp)];
    submit = () => ({ kind, target: exp.value.trim() });
  } else {
    let chosen = null;
    const parts = [];
    for (const d of S.disks) for (const p of d.partitions) if (p.fstype && p.fstype !== "swap" && p.fstype !== "iso9660") parts.push({ d, p });
    const list = h("div", { class: "pick-list" }, parts.length ? parts.map(({ d, p }) => {
      const el = h("div", { class: "img pick", onclick: () => { chosen = p.path; list.querySelectorAll(".pick").forEach((x) => x.classList.remove("selected")); el.classList.add("selected"); } },
        h("div", { class: "img-ico", html: ICONS.disk }),
        h("div", { class: "img-meta" }, h("strong", {}, `${p.path} ${p.label ? "· " + p.label : ""}`), h("div", { class: "disk-sub" }, `${fsName(p.fstype)} · ${d.model || d.path}`)),
        h("div", { class: "disk-size" }, size(p.size)));
      return el;
    }) : h("div", { class: "empty" }, "Aucune partition utilisable. Branchez un disque externe."));
    form = [list];
    submit = () => ({ kind, target: chosen });
  }
  const foot = h("div", { class: "wiz-foot" },
    S.storage.mounted ? h("button", { class: "btn danger-ghost", onclick: () => mount({ kind: "umount" }) }, "Démonter le dépôt actuel") : h("span"),
    h("button", { class: "btn primary", onclick: () => mount(submit()) }, "Monter le dépôt"));
  openModal(wizHead("Dépôt d'images", S.storage.mounted ? `Actuellement : ${S.storage.source} (${S.storage.fstype})` : "Où sont stockées vos images ?"),
    seg, h("div", { style: "height:16px" }), ...form, foot);
}

async function mount(body) {
  if (body.kind !== "umount" && !body.target) return toast("Indiquez l'emplacement du dépôt", true);
  try {
    await api("storage", body);
    closeModal();
    toast(body.kind === "umount" ? "Dépôt démonté" : "Dépôt monté");
    refresh();
  } catch (e) { toast(e.message, true); }
}
$("#chip-storage").onclick = () => storageDialog();

/* ------------------------------------------------------------------ tâche */
const CIRC = 2 * Math.PI * 52;

function renderJob() {
  const job = S && S.job;
  const view = $("#job");
  if (!job || job.id === dismissedJob) { view.hidden = true; return; }
  view.hidden = false;
  view.className = "job " + job.state;
  const p = job.progress || {};
  const done = job.state === "done";
  const pct = done ? 100 : Math.floor((p.overall || 0) * 1000) / 10;
  $("#job-title").textContent = job.title;
  $("#job-pct").textContent = pct.toFixed(pct >= 100 ? 0 : 1).replace(".", ",");
  $("#ring-fg").style.strokeDashoffset = CIRC * (1 - pct / 100);
  const stepPct = p.fraction ? p.fraction * 100 : done ? 100 : 0;
  $("#job-bar").style.width = stepPct + "%";
  $("#job-step-pct").textContent = Math.floor(stepPct) + " %";
  $("#ring-done").hidden = !done;
  document.querySelector(".ring-text").style.visibility = done ? "hidden" : "visible";
  const states = { done: "Terminé avec succès", error: "Échec", cancelled: "Annulé" };
  $("#job-step").textContent = states[job.state] || (p.label ? `${p.label} · étape ${p.step}/${p.steps}` : "Préparation…");
  $("#job-rate").textContent = job.state === "running" ? rate(p.rate) : "—";
  $("#job-eta").textContent = job.state === "running" ? duration(p.eta) : "—";
  $("#job-elapsed").textContent = duration(((job.finished || Date.now() / 1000) - job.started));
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";
  $("#job-cancel").hidden = job.state !== "running";
  $("#job-close").hidden = job.state === "running";
  const log = $("#job-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  const sig = job.logs.length ? job.logs[job.logs.length - 1].t + job.logs.length : 0;
  if (shownJob !== job.id || shownLogs !== sig) {
    log.textContent = job.logs.map((l) => l.msg).join("\n");
    if (atBottom || shownJob !== job.id) log.scrollTop = log.scrollHeight;
    shownLogs = sig;
  }
  shownJob = job.id;
}

$("#job-cancel").onclick = async () => {
  if (!confirm("Annuler l'opération en cours ? Le disque cible sera inutilisable tant qu'une nouvelle copie n'aura pas été faite.")) return;
  try { await api("cancel", {}); } catch (e) { toast(e.message, true); }
};
$("#job-close").onclick = () => { dismissedJob = S.job.id; renderJob(); };

/* ------------------------------------------------------------ alimentation */
$("#btn-power").onclick = (e) => { e.stopPropagation(); $("#power-menu").hidden = !$("#power-menu").hidden; };
document.addEventListener("click", () => ($("#power-menu").hidden = true));
document.querySelectorAll("[data-power]").forEach((b) => (b.onclick = async () => {
  const action = b.dataset.power;
  if (!confirm(action === "reboot" ? "Redémarrer le PC ?" : "Éteindre le PC ?")) return;
  try { await api("power", { action }); toast("À bientôt !"); } catch (e) { toast(e.message, true); }
}));

/* ------------------------------------------------------------------ accès */
function showLogin() { $("#login").hidden = false; $("#login-code").focus(); }
$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  code = $("#login-code").value.trim();
  sessionStorage.setItem("tp-code", code);
  try { S = await api("state"); $("#login").hidden = true; render(); }
  catch (err) { $("#login-error").textContent = "Code incorrect"; }
};

/* ---------------------------------------------------------------- démarrage */
if (!code) showLogin();
refresh().then(loop);
