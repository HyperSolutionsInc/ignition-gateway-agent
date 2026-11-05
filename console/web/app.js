"use strict";
// Gateway fleet console. No framework and no build step. Every value from the API reaches the
// page through textContent (h() below), never innerHTML.

const BASE = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";
const view = document.getElementById("view");
const dialog = document.getElementById("dialog");
let config = { hamf_enabled: false, max_package_mb: 16 };
let refreshTimer = null;

// ------------------------------------------------------------------ helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Console-Request": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + "api/" + path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty or non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || res.status + " " + res.statusText);
  return data;
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.getElementById("toast").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return Math.round(seconds) + "s ago";
  if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.round(seconds / 3600) + "h ago";
  return Math.round(seconds / 86400) + "d ago";
}

function sinceIso(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return isNaN(t) ? null : (Date.now() - t) / 1000;
}

function enc(s) { return encodeURIComponent(s); }

const IMS_TONE = { installed: "ok", downloading: "warn", failed: "bad", removed: "" };

function statusBadge(g) {
  if (g.last_seen === null || g.last_seen === undefined) return h("span", { class: "badge" }, "never seen");
  if (!g.online) return h("span", { class: "badge bad" }, "offline");
  if (g.ignition && g.ignition !== "RUNNING") return h("span", { class: "badge warn" }, "Ignition " + g.ignition);
  if (g.last_error) return h("span", { class: "badge warn", title: g.last_error }, "online, error");
  return h("span", { class: "badge ok" }, "online");
}

function imsChip(key, status) {
  return h("span", { class: "chip " + (IMS_TONE[status] || ""), title: status || "assigned" }, key);
}

function setNav(name) {
  for (const a of document.querySelectorAll("[data-nav]")) a.classList.toggle("active", a.dataset.nav === name);
}

function loading() { view.replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" }), " Loading…")); }

function failed(err) {
  view.replaceChildren(h("div", { class: "card" }, h("div", { class: "card-body" },
    h("h2", null, "Couldn't load this page"), h("p", { class: "error-text" }, err.message),
    h("button", { onclick: route }, "Try again"))));
}

function autoRefresh(fn, ms) {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => { if (!dialog.open && !document.hidden) fn(); }, ms);
}

// ------------------------------------------------------------------ fleet

const fleetState = { selected: new Set(), filter: "" };

async function fleetView() {
  setNav("fleet");
  if (!view.dataset.view || view.dataset.view !== "fleet") loading();
  let data;
  try { data = await api("GET", "gateways"); } catch (e) { return failed(e); }
  view.dataset.view = "fleet";
  const gws = data.gateways;
  const known = new Set(gws.map(g => g.thing));
  for (const t of [...fleetState.selected]) if (!known.has(t)) fleetState.selected.delete(t);

  const online = gws.filter(g => g.online).length;
  const imsTotal = gws.reduce((n, g) => n + g.ims.length, 0);
  const failedIms = gws.reduce((n, g) => n + Object.values(g.ims_status).filter(s => s === "failed").length, 0);
  const backlog = gws.reduce((n, g) => n + (g.outbox_rows || 0), 0);

  const search = h("input", { type: "search", placeholder: "Filter by name, site, customer, IMS…", value: fleetState.filter,
    "aria-label": "Filter gateways", oninput: e => { fleetState.filter = e.target.value; renderRows(); } });
  const rolloutBtn = h("button", { class: "primary", onclick: () => packageDialog({ things: [...fleetState.selected], mode: "rollout" }) });
  const tbody = h("tbody");
  const allBox = h("input", { type: "checkbox", "aria-label": "Select all shown", onchange: e => {
    for (const g of shown()) e.target.checked ? fleetState.selected.add(g.thing) : fleetState.selected.delete(g.thing);
    renderRows();
  } });

  function shown() {
    const q = fleetState.filter.trim().toLowerCase();
    if (!q) return gws;
    return gws.filter(g => [g.thing, g.serial, g.site, g.customer, ...g.ims].some(v => v && String(v).toLowerCase().includes(q)));
  }

  function renderRows() {
    const rows = shown();
    rolloutBtn.disabled = fleetState.selected.size === 0;
    rolloutBtn.textContent = fleetState.selected.size ? "Roll out package to " + fleetState.selected.size : "Roll out package";
    allBox.checked = rows.length > 0 && rows.every(g => fleetState.selected.has(g.thing));
    if (!rows.length) {
      tbody.replaceChildren(h("tr", null, h("td", { colspan: 9, class: "empty" },
        gws.length ? "No gateway matches the filter." : "No gateways yet. A new gateway appears here once it provisions itself.")));
      return;
    }
    tbody.replaceChildren(...rows.map(g => {
      const box = h("input", { type: "checkbox", "aria-label": "Select " + g.thing, onclick: e => e.stopPropagation(),
        onchange: e => { e.target.checked ? fleetState.selected.add(g.thing) : fleetState.selected.delete(g.thing); renderRows(); } });
      box.checked = fleetState.selected.has(g.thing);
      return h("tr", { class: "clickable", onclick: () => { location.hash = "#/gw/" + enc(g.thing); } },
        h("td", null, box),
        h("td", null, h("a", { href: "#/gw/" + enc(g.thing), onclick: e => e.stopPropagation() }, g.thing),
          g.serial ? h("div", { class: "muted mono" }, g.serial) : null),
        h("td", null, g.site || h("span", { class: "muted" }, "—")),
        h("td", null, g.customer || h("span", { class: "muted" }, "—")),
        h("td", null, statusBadge(g)),
        h("td", { class: "wrap" }, g.ims.length ? g.ims.map(k => imsChip(k, g.ims_status[k])) : h("span", { class: "muted" }, "none")),
        h("td", { class: "num" }, g.outbox_rows === null || g.outbox_rows === undefined ? "—" : g.outbox_rows.toLocaleString()),
        h("td", { class: "mono" }, g.agent_version || "—"),
        h("td", null, ago(g.last_seen_age_s)));
    }));
  }

  view.replaceChildren(
    h("div", { class: "pagehead" },
      h("div", { class: "grow" }, h("h1", null, "Fleet"),
        h("div", { class: "sub" }, "Every gateway that has provisioned itself, with its IMSs (PPM packages). Click a gateway to manage it.")),
      h("div", { class: "toolbar" }, search, rolloutBtn, h("button", { onclick: fleetView }, "Refresh"))),
    h("div", { class: "stats" },
      stat("Gateways", gws.length), stat("Online", online + " / " + gws.length),
      stat("IMSs assigned", imsTotal), stat("Failed installs", failedIms), stat("Readings queued", backlog.toLocaleString())),
    h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, allBox), h("th", null, "Gateway"), h("th", null, "Site"), h("th", null, "Customer"),
        h("th", null, "Status"), h("th", null, "IMSs"), h("th", { class: "num" }, "Queued"), h("th", null, "Agent"), h("th", null, "Last report"))),
      tbody))));
  renderRows();
  autoRefresh(fleetView, 15000);
}

function stat(label, value) {
  return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
}

// ------------------------------------------------------------------ one gateway

async function gatewayView(thing) {
  setNav("fleet");
  if (view.dataset.view !== "gw:" + thing) loading();
  let g, audit;
  try {
    [g, audit] = await Promise.all([api("GET", "gateways/" + enc(thing)), api("GET", "audit?thing=" + enc(thing)).catch(() => ({ entries: [] }))]);
  } catch (e) { return failed(e); }
  view.dataset.view = "gw:" + thing;
  const health = (g.gateway_shadow.reported || {}).health || {};
  const paused = g.gateway_shadow.reported.publish_paused === true;

  const act = (action, label, confirmText) => h("button", { onclick: async () => {
    if (confirmText && !(await confirmDialog(label, confirmText, label))) return;
    try { await api("POST", "gateways/" + enc(thing) + "/actions", { action }); toast(label + ": requested"); gatewayView(thing); }
    catch (e) { toast(e.message, true); }
  } }, label);

  const imsRows = g.ims_shadows.filter(s => !(s.reported.status === "removed" && s.desired.remove)).map(s => {
    const key = s.shadow.replace(/^ims-/, "");
    const st = s.desired.remove ? (s.reported.status === "removed" ? "removed" : "removing") : (s.reported.status || "assigned");
    const tone = IMS_TONE[st] !== undefined ? IMS_TONE[st] : "warn";
    return h("tr", null,
      h("td", null, h("strong", null, key)),
      h("td", null, h("span", { class: "badge " + tone }, st), s.reported.error ? h("div", { class: "error-text cell-note" }, s.reported.error) : null),
      h("td", { class: "mono" }, s.reported.package_version || "—", h("div", { class: "muted" }, s.desired.version || "")),
      h("td", { class: "num" }, s.reported.tag_count === undefined ? "—" : s.reported.tag_count.toLocaleString()),
      h("td", null, s.desired.enabled === false || s.reported.enabled === false ? "off" : ((s.desired.interval_ms || s.reported.interval_ms || 1000) / 1000) + " s"),
      h("td", null, s.pending.length ? h("span", { class: "badge warn", title: s.pending.join(", ") }, "pending") : h("span", { class: "muted" }, "in sync")),
      h("td", null, s.reported.applied_at ? ago(sinceIso(s.reported.applied_at)) : "—"),
      h("td", null, h("div", { class: "toolbar" },
        h("button", { class: "small", onclick: () => packageDialog({ things: [thing], mode: "update", ims: key }) }, "Update"),
        h("button", { class: "small", onclick: () => timerDialog(thing, key, s) }, "Timer"),
        st === "failed" ? h("button", { class: "small", onclick: async () => {
          try { await api("POST", "gateways/" + enc(thing) + "/ims/" + enc(key) + "/retry"); toast("Retry requested for " + key); gatewayView(thing); }
          catch (e) { toast(e.message, true); }
        } }, "Retry") : null,
        h("button", { class: "small danger", onclick: async () => {
          if (!(await confirmDialog("Remove " + key, "This deletes " + key + "'s tags and timer script from " + thing + " and stops its telemetry. You can add it back later.", "Remove"))) return;
          try { await api("DELETE", "gateways/" + enc(thing) + "/ims/" + enc(key)); toast(key + " removal requested"); gatewayView(thing); }
          catch (e) { toast(e.message, true); }
        } }, "Remove"))));
  });

  view.replaceChildren(
    h("div", { class: "pagehead" },
      h("div", { class: "grow" },
        h("div", { class: "sub" }, h("a", { href: "#/" }, "Fleet"), " / ", g.thing),
        h("h1", null, g.thing, " ", statusBadge(g)),
        h("div", { class: "sub" }, [g.site, g.customer].filter(Boolean).join(" · ") || "No site or customer set", " ",
          h("button", { class: "small", onclick: () => labelDialog(g) }, "Edit"))),
      h("div", { class: "toolbar" },
        h("button", { class: "primary", onclick: () => packageDialog({ things: [thing], mode: "add" }) }, "Add PPM"),
        paused ? act("resume", "Resume telemetry") : act("pause", "Pause telemetry", "The gateway keeps collecting and buffering readings but stops sending them until resumed."),
        act("rescan", "Rescan projects"),
        act("restart", "Restart Ignition", "Asks the gateway to restart Ignition. It's refused unless the gateway has ALLOW_REMOTE_RESTART on."),
        h("button", { onclick: () => gatewayView(thing) }, "Refresh"))),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, "IMSs (PPM packages)"), h("span", { class: "muted" }, imsRows.length + " assigned")),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "IMS"), h("th", null, "Status"), h("th", null, "Package / label"), h("th", { class: "num" }, "Tags"),
          h("th", null, "Timer"), h("th", null, "Shadow"), h("th", null, "Applied"), h("th", null, ""))),
        h("tbody", null, imsRows.length ? imsRows : h("tr", null, h("td", { colspan: 8, class: "empty" }, "No IMSs yet. Use Add PPM to assign one."))))))
    ,
    h("div", { class: "grid2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Health")), h("div", { class: "card-body" },
        h("dl", { class: "kv" },
          kv("Last report", health.reported_at ? health.reported_at + " (" + ago(g.last_seen_age_s) + ")" : "never"),
          kv("Agent", health.agent_version), kv("Ignition", (health.ignition || {}).state),
          kv("Connected since", (health.link || {}).connected_since), kv("Last disconnect", (health.link || {}).last_disconnect),
          kv("Telemetry topic", (health.link || {}).telemetry_topic, true),
          kv("Telemetry", paused ? "paused" : "sending"),
          kv("Messages sent", health.published_messages), kv("Last publish", health.last_publish),
          kv("Queued readings", (health.outbox || {}).rows), kv("Oldest queued", (health.outbox || {}).oldest_age_s !== undefined ? (health.outbox || {}).oldest_age_s + " s" : null),
          kv("Dropped (buffer full)", (health.outbox || {}).dropped_rows_total),
          kv("Disk free", health.disk_free_mb !== undefined ? Math.round(health.disk_free_mb / 1024) + " GB" : null),
          kv("Last error", health.last_error)))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Identity")), h("div", { class: "card-body" },
        h("dl", { class: "kv" }, kv("Thing", g.thing, true), kv("Serial", g.serial, true),
          ...Object.entries(g.attributes).filter(([k]) => !["serial", "site", "customer"].includes(k)).map(([k, v]) => kv(k, v))),
        h("h2", { class: "mt16" }, "Certificates"),
        g.certificates.length ? h("dl", { class: "kv" }, ...g.certificates.map(c => kv(c.id, c.status + (c.not_after ? " · valid to " + String(c.not_after).slice(0, 10) : ""), true)))
          : h("div", { class: "muted" }, "No certificate attached"),
        g.gateway_shadow.pending.length ? h("p", { class: "muted" }, "Waiting on the gateway for: " + g.gateway_shadow.pending.join(", ")) : null))),
    h("div", { class: "stack mt20" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Activity")),
        auditTable(audit.entries, false)),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Raw shadows")), h("div", { class: "card-body" },
        h("details", null, h("summary", null, "gateway"), h("pre", { class: "json" }, JSON.stringify(g.gateway_shadow, null, 2))),
        ...g.ims_shadows.map(s => h("details", null, h("summary", null, s.shadow + " (version " + s.shadow_version + ")"),
          h("pre", { class: "json" }, JSON.stringify({ desired: s.desired, reported: s.reported, pending: s.pending }, null, 2)))))),
      h("div", { class: "card danger" }, h("div", { class: "card-head" }, h("h2", null, "Decommission")), h("div", { class: "card-body" },
        h("p", { class: "muted" }, "Revokes this gateway's certificate (it can never connect again), deletes its shadows and removes the thing. Its packages stay in S3. To bring the hardware back, wipe the gateway-agent volume so it provisions again."),
        h("button", { class: "danger", onclick: () => decommissionDialog(g.thing) }, "Decommission " + g.thing)))));
  autoRefresh(() => gatewayView(thing), 15000);
}

function kv(k, v, mono) {
  return [h("dt", null, k), h("dd", { class: mono ? "mono" : null }, v === null || v === undefined || v === "" ? "—" : String(v))];
}

// ------------------------------------------------------------------ activity

function auditTable(entries, withThing) {
  if (!entries.length) return h("div", { class: "empty" }, "Nothing yet.");
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "When (UTC)"), h("th", null, "Action"), withThing ? h("th", null, "Gateway") : null, h("th", null, "Details"), h("th", null, "From"))),
    h("tbody", null, entries.map(e => h("tr", null,
      h("td", { class: "mono" }, e.at.replace("T", " ").replace("Z", "")),
      h("td", null, h("strong", null, e.action)),
      withThing ? h("td", null, e.thing ? h("a", { href: "#/gw/" + enc(e.thing) }, e.thing) : h("span", { class: "muted" }, "fleet")) : null,
      h("td", { class: "wrap mono" }, Object.entries(e.detail || {}).map(([k, v]) => k + "=" + (Array.isArray(v) ? v.join(",") : v)).join("  ")),
      h("td", { class: "mono muted" }, e.ip))))));
}

async function activityView() {
  setNav("activity");
  loading();
  let data;
  try { data = await api("GET", "audit"); } catch (e) { return failed(e); }
  view.dataset.view = "activity";
  view.replaceChildren(
    h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Activity"),
      h("div", { class: "sub" }, "Every change made through this console, newest first (kept 400 days)."))),
    h("div", { class: "card" }, auditTable(data.entries, true)));
  clearInterval(refreshTimer);
}

// ------------------------------------------------------------------ dialogs

function openDialog(title, body, foot) {
  dialog.replaceChildren(
    h("div", { class: "dlg-head" }, h("h2", null, title), h("button", { class: "small", "aria-label": "Close", onclick: () => dialog.close() }, "✕")),
    h("div", { class: "dlg-body" }, body),
    h("div", { class: "dlg-foot" }, foot));
  if (!dialog.open) dialog.showModal();
}

function confirmDialog(title, text, okLabel) {
  return new Promise(resolve => {
    const done = v => { dialog.close(); resolve(v); };
    openDialog(title, h("p", { class: "m0" }, text),
      [h("button", { onclick: () => done(false) }, "Cancel"), h("button", { class: "primary", onclick: () => done(true) }, okLabel)]);
    dialog.addEventListener("close", () => resolve(false), { once: true });
  });
}

function labelDialog(g) {
  const site = h("input", { type: "text", value: g.site || "", placeholder: "e.g. Petersburg" });
  const customer = h("input", { type: "text", value: g.customer || "", placeholder: "e.g. Switch" });
  openDialog("Labels for " + g.thing,
    [h("label", { class: "field" }, "Site", site), h("label", { class: "field" }, "Customer", customer),
     h("p", { class: "muted m0" }, "Stored as attributes on the AWS IoT thing. Leave a field empty to clear it.")],
    [h("button", { onclick: () => dialog.close() }, "Cancel"), h("button", { class: "primary", onclick: async () => {
      try { await api("PATCH", "gateways/" + enc(g.thing), { site: site.value, customer: customer.value }); dialog.close(); toast("Labels saved"); gatewayView(g.thing); }
      catch (e) { toast(e.message, true); }
    } }, "Save")]);
}

function timerDialog(thing, key, s) {
  const interval = h("input", { type: "number", min: 100, max: 3600000, step: 100, value: s.desired.interval_ms || s.reported.interval_ms || 1000 });
  const enabled = h("input", { type: "checkbox" });
  enabled.checked = !(s.desired.enabled === false || s.reported.enabled === false);
  openDialog("Timer for " + key,
    [h("label", { class: "field" }, "Read interval (ms)", interval),
     h("label", { class: "check" }, enabled, "Timer enabled (telemetry for this IMS)"),
     h("p", { class: "muted m0" }, "Changes the timer script on the gateway without reinstalling the package.")],
    [h("button", { onclick: () => dialog.close() }, "Cancel"), h("button", { class: "primary", onclick: async () => {
      try { await api("PATCH", "gateways/" + enc(thing) + "/ims/" + enc(key), { interval_ms: Number(interval.value), enabled: enabled.checked });
        dialog.close(); toast("Timer change sent to " + key); gatewayView(thing); }
      catch (e) { toast(e.message, true); }
    } }, "Apply")]);
}

function decommissionDialog(thing) {
  const input = h("input", { type: "text", placeholder: thing, "aria-label": "Type the gateway name" });
  const go = h("button", { class: "danger solid", disabled: true, onclick: async () => {
    go.disabled = true;
    try { const r = await api("DELETE", "gateways/" + enc(thing), { confirm: input.value }); dialog.close();
      toast(thing + " decommissioned (" + r.revoked_certificates.length + " certificate revoked)"); location.hash = "#/"; }
    catch (e) { toast(e.message, true); go.disabled = false; }
  } }, "Decommission");
  input.addEventListener("input", () => { go.disabled = input.value !== thing; });
  openDialog("Decommission " + thing,
    [h("p", { class: "m0" }, "This can't be undone from the console. The certificate is revoked, and the thing and its shadows are deleted."),
     h("label", { class: "field" }, "Type " + thing + " to confirm", input)],
    [h("button", { onclick: () => dialog.close() }, "Cancel"), go]);
}

// Add a PPM to one gateway, update one, or roll one package out to several gateways.
function packageDialog({ things, mode, ims }) {
  const st = { source: "upload", uploadKey: null, hamfId: "", preview: null, busy: false };
  const titles = { add: "Add PPM to " + things[0], update: "Update " + ims + " on " + things[0], rollout: "Roll out a package to " + things.length + " gateway" + (things.length > 1 ? "s" : "") };
  const fileInput = h("input", { type: "file", accept: ".json,.zip,application/json,application/zip", hidden: true, onchange: e => e.target.files[0] && upload(e.target.files[0]) });
  const drop = h("div", { class: "drop", tabindex: 0, role: "button", onclick: () => fileInput.click(), onkeydown: e => { if (e.key === "Enter" || e.key === " ") fileInput.click(); } },
    h("strong", null, "Drop a HAMF export here"), h("div", null, "or click to choose a .json or .zip (up to " + config.max_package_mb + " MB)"));
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", e => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });

  const hamfInput = h("input", { type: "text", inputmode: "numeric", placeholder: "HAMF asset id of the IMS, e.g. 1234" });
  const hamfBtn = h("button", { onclick: () => { st.hamfId = hamfInput.value.trim(); runPreview({ hamf_asset_id: st.hamfId }); } }, "Fetch from HAMF");
  const hamfPane = config.hamf_enabled
    ? h("div", { class: "row" }, h("label", { class: "field grow1" }, "HAMF asset id", hamfInput), hamfBtn)
    : h("p", { class: "muted m0" }, "HAMF pull isn't switched on for this console yet (it needs HAMF reachable from AWS: set HamfApiUrl on the stack). Upload the export file instead.");
  const sourcePane = h("div");
  const seg = h("div", { class: "seg", role: "tablist" });
  const previewBox = h("div");
  const interval = h("input", { type: "number", min: 100, max: 3600000, step: 100, placeholder: "keep current", title: "Leave empty to keep the gateway's current interval (default 1000 ms)" });
  const enabled = h("select", null, h("option", { value: "" }, "keep current"), h("option", { value: "true" }, "on"), h("option", { value: "false" }, "off"));
  const publishBtn = h("button", { class: "primary", disabled: true, onclick: publish }, mode === "rollout" ? "Roll out" : "Publish to gateway");

  function renderSource() {
    seg.replaceChildren(
      h("button", { class: st.source === "upload" ? "on" : "", role: "tab", onclick: () => { st.source = "upload"; renderSource(); } }, "Upload file"),
      h("button", { class: st.source === "hamf" ? "on" : "", role: "tab", onclick: () => { st.source = "hamf"; renderSource(); } }, "From HAMF"));
    sourcePane.replaceChildren(st.source === "upload" ? drop : hamfPane);
  }

  async function upload(file) {
    if (file.size > config.max_package_mb * 1024 * 1024) return toast("That file is over " + config.max_package_mb + " MB", true);
    previewBox.replaceChildren(h("div", { class: "preview" }, h("span", { class: "spinner" }), " Uploading " + file.name + "…"));
    try {
      const up = await api("POST", "uploads", { filename: file.name, size: file.size });
      const put = await fetch(up.url, { method: "PUT", body: file });
      if (!put.ok) throw new Error("upload to S3 failed (" + put.status + ")");
      st.uploadKey = up.upload_key;
      await runPreview({ upload_key: up.upload_key });
    } catch (e) { previewBox.replaceChildren(h("div", { class: "preview error-text" }, e.message)); }
  }

  async function runPreview(src) {
    st.preview = null; publishBtn.disabled = true;
    previewBox.replaceChildren(h("div", { class: "preview" }, h("span", { class: "spinner" }), " Checking the export…"));
    try {
      const body = Object.assign({}, src);
      if (things.length === 1) body.thing = things[0];
      const p = await api("POST", "preview", body);
      st.preview = p; st.src = src;
      const mismatch = mode === "update" && p.ims_key !== ims;
      const d = p.diff;
      previewBox.replaceChildren(h("div", { class: "preview" },
        h("dl", { class: "kv" }, kv("IMS", p.ims_key), kv("Tags", p.tag_count.toLocaleString()), kv("Devices", p.devices.length + ": " + p.devices.slice(0, 12).join(", ") + (p.devices.length > 12 ? "…" : "")),
          kv("Tag folder", p.tag_folder, true), kv("Package", p.version + " · " + (p.size / 1024).toFixed(0) + " KB", true)),
        mismatch ? h("p", { class: "error-text" }, "This export is for " + p.ims_key + ", not " + ims + ". Publishing adds it as a separate IMS.") : null,
        d ? h("div", null, h("div", { class: "diffline" },
              d.same ? h("span", { class: "muted" }, "Same tags as the current package (" + d.from_version + ").") : [
                h("span", { class: "plus" }, "+" + d.added + " tags"), h("span", { class: "minus" }, "−" + d.removed + " tags"), h("span", { class: "moved" }, d.moved + " moved")],
              d.devices_added.length ? h("span", { class: "plus" }, "new devices: " + d.devices_added.join(", ")) : null,
              d.devices_removed.length ? h("span", { class: "minus" }, "devices dropped: " + d.devices_removed.join(", ")) : null),
            d.removed ? h("details", null, h("summary", null, "Tags that will be deleted"), h("pre", { class: "json" }, d.removed_sample.join("\n") + (d.removed > 20 ? "\n…" : ""))) : null,
            d.added ? h("details", null, h("summary", null, "Tags that will be added"), h("pre", { class: "json" }, d.added_sample.join("\n") + (d.added > 20 ? "\n…" : ""))) : null)
          : (things.length === 1 ? h("p", { class: "muted mt8" }, mode === "update" ? "No earlier console upload to compare with." : "New IMS on this gateway.") : null)));
      publishBtn.disabled = false;
    } catch (e) { previewBox.replaceChildren(h("div", { class: "preview error-text" }, e.message)); }
  }

  async function publish() {
    if (!st.preview) return;
    const body = Object.assign({}, st.src);
    if (interval.value) body.interval_ms = Number(interval.value);
    if (enabled.value) body.enabled = enabled.value === "true";
    publishBtn.disabled = true; publishBtn.textContent = "Publishing…";
    try {
      if (mode === "rollout") {
        body.things = things;
        const r = await api("POST", "rollout", body);
        const bad = r.results.filter(x => !x.ok);
        dialog.close();
        toast(bad.length ? r.ims + ": sent to " + (r.results.length - bad.length) + ", failed on " + bad.map(x => x.thing).join(", ") : r.ims + " sent to " + r.results.length + " gateways", bad.length > 0);
        fleetState.selected.clear(); fleetView();
      } else {
        const r = await api("POST", "gateways/" + enc(things[0]) + "/ims", body);
        dialog.close(); toast(r.ims + " sent to " + things[0] + ". It installs within seconds if the gateway is online.");
        gatewayView(things[0]);
      }
    } catch (e) { toast(e.message, true); publishBtn.disabled = false; publishBtn.textContent = mode === "rollout" ? "Roll out" : "Publish to gateway"; }
  }

  renderSource();
  openDialog(titles[mode], [
    mode === "rollout" ? h("p", { class: "muted m0" }, "To: " + things.join(", ")) : null,
    seg, sourcePane, fileInput, previewBox,
    h("div", { class: "row" }, h("label", { class: "field" }, "Read interval (ms)", interval), h("label", { class: "field" }, "Timer", enabled))],
    [h("button", { onclick: () => dialog.close() }, "Cancel"), publishBtn]);
}

// ------------------------------------------------------------------ routing

function route() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/gw\/(.+)$/);
  if (m) return gatewayView(decodeURIComponent(m[1]));
  if (hash === "#/activity") return activityView();
  return fleetView();
}

window.addEventListener("hashchange", () => { view.dataset.view = ""; route(); });
api("GET", "config").then(c => {
  config = c;
  document.getElementById("env").textContent = c.region + " · " + c.thing_prefix + "*";
}).catch(() => {}).finally(route);
