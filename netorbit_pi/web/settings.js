/* Settings: what the integration's Configure menu did, as one page. */
(function () {
  const { esc, api, toast, rel, statusLine } = window.NO;
  const HUB_LABEL = { router: "Router", switch: "Switch", access_point: "Access point", mesh_node: "Mesh node", other: "Other" };
  const PLACED = { manual: "by you", snmp: "switch table", controller: "controller", none: "not known" };
  const ICON_CHOICES = Object.keys(window.NETORBIT_ICONS || {}).filter((n) => !/^(close|cog|logout|content-save|magnify|refresh|plus|delete|download|upload|cancel|check|pencil|history|dots-horizontal-circle-outline|tag-plus-outline|help-circle-outline|alert|radar)$/.test(n));
  let S = null;

  // ----- tabs -----
  const tabs = document.querySelectorAll("#tabs button");
  function show(tab) {
    tabs.forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== tab));
    if (location.hash.slice(1) !== tab) history.replaceState(null, "", "#" + tab);
  }
  tabs.forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
  function fromHash() {
    const want = location.hash.slice(1) || "network";
    show([...tabs].some((b) => b.dataset.tab === want) ? want : "network");
  }
  window.addEventListener("hashchange", fromHash);
  fromHash();

  async function load() {
    S = await api("/api/settings");
    statusLine(document.getElementById("status"), S.status);
    document.getElementById("logout").hidden = !S.status.password_set;
    fillNetwork();
    drawHubs();
    drawDevices();
    fillPaste();
    fillNotify();
    fillSecurity();
    const n = (S.suggested_hubs || []).length + (S.new_devices || []).length;
    tabs.forEach((b) => {
      if (b.dataset.tab === "hubs") b.innerHTML = `Hubs${S.suggested_hubs.length ? `<span class="count">${S.suggested_hubs.length}</span>` : ""}`;
      if (b.dataset.tab === "devices") b.innerHTML = `Devices${S.new_devices.length ? `<span class="count">${S.new_devices.length}</span>` : ""}`;
    });
    return n;
  }

  async function run(service, data, okMsg) {
    try {
      const r = await api(`/api/services/${service}`, data);
      if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg);
      await load();
      return r;
    } catch (e) {
      toast(e.message, true);
      throw e;
    }
  }

  // ----- network -----
  const fNet = document.getElementById("f-network");
  function fillNetwork() {
    const o = S.options;
    fNet.subnet.value = S.data.subnet || "";
    fNet.gateway.value = S.data.gateway || "";
    fNet.mode.value = o.mode || "auto";
    fNet.ping_interval.value = o.ping_interval ?? 60;
    fNet.discovery_interval.value = o.discovery_interval ?? 300;
    fNet.stale_hours.value = o.stale_hours ?? 24;
    fNet.ignore_random_mac.checked = o.ignore_random_mac !== false;
    fNet.auto_hubs.checked = o.auto_hubs !== false;
    fNet.group_by_type.checked = o.group_by_type !== false;
    fNet.snmp_enabled.checked = o.snmp_enabled !== false;
    fNet.snmp_community.value = o.snmp_community || "";
  }
  fNet.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/settings", {
        subnet: fNet.subnet.value.trim(), gateway: fNet.gateway.value.trim(), mode: fNet.mode.value,
        ping_interval: fNet.ping_interval.value, discovery_interval: fNet.discovery_interval.value,
        stale_hours: fNet.stale_hours.value, ignore_random_mac: fNet.ignore_random_mac.checked,
        auto_hubs: fNet.auto_hubs.checked, group_by_type: fNet.group_by_type.checked,
        snmp_enabled: fNet.snmp_enabled.checked, snmp_community: fNet.snmp_community.value.trim(),
      });
      toast("Saved");
      await load();
    } catch (err) { toast(err.message, true); }
  });

  // ----- hubs -----
  function hubName(id) {
    const h = S.hubs.find((x) => x.id === id);
    return h ? h.name : id || "";
  }
  function hubOptions(selected, exclude, withAuto) {
    return (withAuto ? `<option value="">Automatic</option>` : "") +
      S.hubs.filter((h) => h.id !== exclude).map((h) => `<option value="${esc(h.id)}" ${h.id === selected ? "selected" : ""}>${esc(h.name)}</option>`).join("");
  }
  function typeOptions(selected, noRouter) {
    return Object.entries(HUB_LABEL).filter(([k]) => !(noRouter && k === "router"))
      .map(([k, v]) => `<option value="${k}" ${k === selected ? "selected" : ""}>${v}</option>`).join("");
  }
  function drawHubs() {
    const sug = S.suggested_hubs || [];
    document.getElementById("suggested-panel").hidden = !sug.length;
    document.getElementById("t-suggested").innerHTML = `<tr><th>Device</th><th class="hide-sm">Why</th><th>Type</th><th></th></tr>` +
      sug.map((s, i) => `<tr><td>${esc(s.name)}<div class="sub">${esc(s.ip || "")} ${esc(s.mac || "")}</div></td>
        <td class="hide-sm sub">${esc((s.reasons || []).join(", "))}</td>
        <td><select class="inline-input" data-i="${i}">${typeOptions(s.type || "access_point", true)}</select></td>
        <td style="white-space:nowrap"><button class="btn small go" data-yes="${i}">It's a hub</button> <button class="btn small" data-no="${i}">Not a hub</button></td></tr>`).join("");
    document.querySelectorAll("#t-suggested [data-yes]").forEach((b) => b.addEventListener("click", () => {
      const s = sug[b.dataset.yes];
      const type = document.querySelector(`#t-suggested select[data-i="${b.dataset.yes}"]`).value;
      run("confirm_hub", { device: s.key, type }, `${s.name} is now a hub`);
    }));
    document.querySelectorAll("#t-suggested [data-no]").forEach((b) => b.addEventListener("click", () => {
      const s = sug[b.dataset.no];
      run("dismiss_hub", { device: s.key }, `Won't suggest ${s.name} again`);
    }));

    const icon = (t) => ({ router: "router-network", switch: "switch", access_point: "access-point", mesh_node: "access-point-network" }[t] || "lan");
    document.getElementById("t-hubs").innerHTML = `<tr><th></th><th>Name</th><th>Type</th><th class="hide-sm">Address</th><th>Connects through</th><th></th></tr>` +
      S.hubs.map((h) => `<tr><td><ha-icon icon="mdi:${icon(h.type)}"></ha-icon></td><td>${esc(h.name)}<div class="sub">${esc(h.source === "manual" || !h.source ? "added by you" : "found automatically")}</div></td>
        <td>${esc(HUB_LABEL[h.type] || "Hub")}</td><td class="hide-sm sub">${esc(h.host || "no IP")}<br>${esc((h.mac || "").toUpperCase())}</td>
        <td>${h.id === "gateway" ? "—" : esc(hubName(h.link || h.parent || "gateway"))}${h.parent_locked ? ` <span class="tag">pinned</span>` : ""}</td>
        <td><button class="btn small" data-edit="${esc(h.id)}">Edit</button></td></tr>`).join("");
    document.querySelectorAll("#t-hubs [data-edit]").forEach((b) => b.addEventListener("click", () => editHub(b.dataset.edit)));
  }

  function modal(html) {
    const bg = document.createElement("div");
    bg.className = "modal-bg";
    bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
    document.body.appendChild(bg);
    const close = () => bg.remove();
    bg.addEventListener("click", (e) => { if (e.target === bg) close(); });
    bg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
    const first = bg.querySelector("input, select, textarea");
    if (first) first.focus();
    return { el: bg, close, q: (s) => bg.querySelector(s) };
  }

  function editHub(id) {
    const h = S.hubs.find((x) => x.id === id);
    const root = id === "gateway";
    const m = modal(`<h2>${esc(h.name)}</h2>
      <label class="field"><span>Name</span><input type="text" class="name" value="${esc(h.name)}"></label>
      ${root ? "" : `<label class="field"><span>Type</span><select class="type">${typeOptions(h.type, true)}</select></label>
      <label class="field"><span>Connects through</span><select class="parent">${hubOptions(h.link || h.parent || "gateway", id)}</select><small>Setting this pins it; switch tables won't move it.</small></label>`}
      <div class="actions"><button class="btn go save">Save</button>${root ? "" : `<button class="btn warn rm">Remove</button>`}<button class="btn" data-close>Cancel</button></div>`);
    m.q(".save").addEventListener("click", async () => {
      const data = { hub: id, name: m.q(".name").value.trim() };
      if (!root) {
        data.type = m.q(".type").value;
        if (m.q(".parent").value !== (h.link || h.parent || "gateway")) data.parent = m.q(".parent").value;
      }
      try { await run("update_hub", data, "Saved"); m.close(); } catch (e) { /* toast shown */ }
    });
    if (m.q(".rm")) m.q(".rm").addEventListener("click", async () => {
      if (!confirm(`Remove ${h.name}? Its devices move to the hub above it.`)) return;
      try { await run("remove_hub", { hub: id }, "Removed"); m.close(); } catch (e) { /* toast shown */ }
    });
  }

  document.getElementById("add-hub").addEventListener("click", () => {
    const m = modal(`<h2>Add a hub</h2>
      <label class="field"><span>Name</span><input type="text" class="name" placeholder="Upstairs access point"></label>
      <label class="field"><span>Type</span><select class="type">${typeOptions("access_point", true)}</select></label>
      <label class="field"><span>IP address</span><input type="text" class="ip" placeholder="Optional"><small>Leave empty for gear without an IP, like an unmanaged switch.</small></label>
      <label class="field"><span>MAC address</span><input type="text" class="mac" placeholder="Optional"></label>
      <label class="field"><span>Connects through</span><select class="parent">${hubOptions("gateway")}</select></label>
      <div class="actions"><button class="btn go save">Add</button><button class="btn" data-close>Cancel</button></div>`);
    m.q(".save").addEventListener("click", async () => {
      const data = { name: m.q(".name").value.trim(), type: m.q(".type").value, parent: m.q(".parent").value };
      if (m.q(".ip").value.trim()) data.ip = m.q(".ip").value.trim();
      if (m.q(".mac").value.trim()) data.mac = m.q(".mac").value.trim();
      try { await run("add_hub", data, "Hub added"); m.close(); } catch (e) { /* toast shown */ }
    });
  });

  // ----- devices -----
  const filterEl = document.getElementById("filter");
  filterEl.addEventListener("input", () => drawDevices());
  function drawDevices() {
    const f = filterEl.value.trim().toLowerCase();
    const list = S.devices.filter((d) => !f || [d.name, d.ip, d.mac, d.vendor].some((x) => String(x || "").toLowerCase().includes(f)));
    document.getElementById("t-devices").innerHTML = `<tr><th></th><th>Name</th><th class="hide-sm">Address</th><th>Hub</th><th></th></tr>` +
      (list.length ? list.map((d) => `<tr><td><ha-icon icon="${esc(d.icon)}"></ha-icon></td>
        <td><span class="st ${esc(d.status)}"></span> ${esc(d.name)}${d.sleepy ? ` <span class="tag">sleeps</span>` : ""}<div class="sub">${esc(d.vendor || "")}</div></td>
        <td class="hide-sm sub">${esc(d.ip || "")}<br>${esc(d.mac || "")}</td>
        <td>${esc(hubName(d.hub))}<div class="sub">${esc(PLACED[d.placement] || "")}</div></td>
        <td><button class="btn small" data-dev="${esc(d.key)}">Edit</button></td></tr>`).join("")
        : `<tr><td colspan="5" class="sub">${S.devices.length ? "Nothing matches." : "No devices yet."}</td></tr>`);
    document.querySelectorAll("#t-devices [data-dev]").forEach((b) => b.addEventListener("click", () => editDevice(b.dataset.dev)));

    const fresh = S.new_devices || [];
    document.getElementById("new-panel").hidden = !fresh.length;
    document.getElementById("t-new").innerHTML = `<tr><th>Device</th><th class="hide-sm">First seen</th><th></th></tr>` +
      fresh.map((d, i) => `<tr><td>${esc(d.name)}<div class="sub">${esc(d.ip || "")} ${esc(d.mac || "")} ${esc(d.vendor || "")}</div></td>
        <td class="hide-sm sub">${esc(rel(d.first_seen))}</td>
        <td style="white-space:nowrap"><button class="btn small go" data-track="${i}">Track</button> <button class="btn small" data-ign="${i}">Ignore</button></td></tr>`).join("");
    document.querySelectorAll("#t-new [data-track]").forEach((b) => b.addEventListener("click", () => {
      const d = fresh[b.dataset.track];
      run("track_device", { mac: d.mac || undefined, ip: d.ip || undefined }, `Tracking ${d.name}`);
    }));
    document.querySelectorAll("#t-new [data-ign]").forEach((b) => b.addEventListener("click", () => {
      const d = fresh[b.dataset.ign];
      run("ignore_device", { mac: d.mac || undefined, ip: d.ip || undefined }, `Ignoring ${d.name}`);
    }));

    const ign = S.ignored || [];
    document.getElementById("ignored-panel").hidden = !ign.length;
    document.getElementById("t-ignored").innerHTML = ign.map((d, i) => `<tr><td>${esc((d.mac || d.key || "").toUpperCase())}<div class="sub">${esc(d.ip || "")} ${esc(d.vendor || "")} ${esc(d.hostname || "")}</div></td>
      <td><button class="btn small" data-restore="${i}">Track again</button></td></tr>`).join("");
    document.querySelectorAll("#t-ignored [data-restore]").forEach((b) => b.addEventListener("click", () => {
      const d = ign[b.dataset.restore];
      const key = d.key || "";
      const data = key.startsWith("ip:") ? { ip: key.slice(3) } : { mac: d.mac || key };
      run("track_device", data, "Tracking again");
    }));
  }

  function editDevice(key) {
    const d = S.devices.find((x) => x.key === key);
    let chosen = d.custom_icon || "";
    const ref = d.mac || d.ip || key;
    const m = modal(`<h2>${esc(d.name)}</h2>
      <label class="field"><span>Name</span><input type="text" class="name" value="${esc(d.custom_name || "")}" placeholder="${esc(d.name)}"></label>
      <div class="field"><span>Icon</span><div class="icon-picker">${ICON_CHOICES.map((n) => `<button type="button" title="${n}" data-icon="mdi:${n}" class="${"mdi:" + n === (chosen || d.icon) ? "on" : ""}"><ha-icon icon="mdi:${n}"></ha-icon></button>`).join("")}</div></div>
      <label class="field"><span>Connects through</span><select class="hub">${hubOptions(d.placement === "manual" ? d.hub : "", null, true)}</select><small>Automatic lets switch tables and controllers decide.</small></label>
      <label class="check"><input type="checkbox" class="sleepy" ${d.sleepy ? "checked" : ""}><span>Allowed to sleep<small>For TVs, consoles and anything that's off a lot: never reported as gone.</small></span></label>
      <div class="actions"><button class="btn go save">Save</button><button class="btn warn ign">Stop tracking</button><button class="btn" data-close>Cancel</button></div>`);
    m.el.querySelectorAll("[data-icon]").forEach((b) => b.addEventListener("click", () => {
      chosen = b.dataset.icon;
      m.el.querySelectorAll("[data-icon]").forEach((x) => x.classList.toggle("on", x === b));
    }));
    m.q(".save").addEventListener("click", async () => {
      const hub = m.q(".hub").value;
      const data = { device: ref, sleepy: m.q(".sleepy").checked };
      const name = m.q(".name").value.trim();
      if (name) data.name = name;
      if (chosen) data.icon = chosen;
      try {
        if (hub) data.hub = hub;
        await api("/api/services/update_device", data);
        if (!hub && d.placement === "manual") await api("/api/services/reset_placement", { device: [ref] });
        toast("Saved");
        await load();
        m.close();
      } catch (e) { toast(e.message, true); }
    });
    m.q(".ign").addEventListener("click", async () => {
      try { await run("ignore_device", { device: ref }, `Stopped tracking ${d.name}`); m.close(); } catch (e) { /* toast shown */ }
    });
  }

  document.getElementById("add-device").addEventListener("click", () => {
    const m = modal(`<h2>Add a device</h2>
      <p class="small">For something the sweep can't see, or to name a device before it shows up.</p>
      <label class="field"><span>MAC address</span><input type="text" class="mac"></label>
      <label class="field"><span>or IP address</span><input type="text" class="ip"></label>
      <label class="field"><span>Name</span><input type="text" class="name"></label>
      <label class="field"><span>Connects through</span><select class="hub">${hubOptions("", null, true)}</select></label>
      <div class="actions"><button class="btn go save">Add</button><button class="btn" data-close>Cancel</button></div>`);
    m.q(".save").addEventListener("click", async () => {
      const data = {};
      ["mac", "ip", "name", "hub"].forEach((k) => { const v = m.q("." + k).value.trim(); if (v) data[k] = v; });
      if (!data.mac && !data.ip) { toast("Give a MAC or IP address", true); return; }
      try { await run("track_device", data, "Added"); m.close(); } catch (e) { /* toast shown */ }
    });
  });

  // ----- paste -----
  const fPaste = document.getElementById("f-paste");
  function fillPaste() {
    const cur = fPaste.hub.value;
    fPaste.hub.innerHTML = `<option value="" disabled ${cur ? "" : "selected"}>Choose a hub…</option>` + hubOptions(cur);
  }
  fPaste.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = document.getElementById("paste-result");
    try {
      const r = await api("/api/services/import_client_list", { hub: fPaste.hub.value, text: fPaste.text.value, add_missing: fPaste.add_missing.checked });
      out.innerHTML = `Placed ${r.assigned.length} under ${esc(hubName(r.hub))}: ${esc(r.assigned.join(", "))}` +
        (r.added_new.length ? `<br>Added ${r.added_new.length} new: ${esc(r.added_new.join(", "))}` : "") +
        (r.not_matched.length ? `<br>Not matched: ${esc(r.not_matched.join(", "))}` : "");
      fPaste.text.value = "";
      await load();
    } catch (err) {
      out.textContent = err.message;
    }
  });

  // ----- notifications -----
  const fNotify = document.getElementById("f-notify");
  const EVENTS = ["netorbit_new_device", "netorbit_device_stale", "netorbit_device_recovered"];
  function fillNotify() {
    fNotify.ntfy_url.value = S.options.ntfy_url || "";
    fNotify.webhook_url.value = S.options.webhook_url || "";
    EVENTS.forEach((ev) => (fNotify["ev_" + ev].checked = (S.options.notify_events || []).includes(ev)));
    fNotify.history_days.value = S.options.history_days || 90;
  }
  fNotify.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/settings", {
        ntfy_url: fNotify.ntfy_url.value.trim(), webhook_url: fNotify.webhook_url.value.trim(),
        notify_events: EVENTS.filter((ev) => fNotify["ev_" + ev].checked), history_days: fNotify.history_days.value,
      });
      toast("Saved");
      await load();
    } catch (err) { toast(err.message, true); }
  });
  document.getElementById("test-notify").addEventListener("click", async () => {
    try { await api("/api/test_notify", {}); toast("Test sent"); } catch (e) { toast(e.message, true); }
  });

  // ----- password and backup -----
  const fPw = document.getElementById("f-password");
  function fillSecurity() {
    const set = S.status.password_set;
    document.getElementById("pw-current-wrap").hidden = !set;
    document.getElementById("pw-lead").textContent = set
      ? "A password is set. Everyone needs it to see or change the map."
      : "No password yet: anyone who can reach this Pi can see and change the map. Set one to require a login.";
    document.getElementById("about").textContent = `NetOrbit for Raspberry Pi ${S.status.version}. Based on NetOrbit for Home Assistant by the-scott-davis (MIT).`;
  }
  fPw.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/password", { current: fPw.current.value, new: fPw.new.value });
      fPw.reset();
      toast(r.password_set ? "Password saved" : "Password removed");
      await load();
    } catch (err) { toast(err.message, true); }
  });

  document.getElementById("export").addEventListener("click", async () => {
    try {
      const data = await api("/api/services/export_config", {});
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `netorbit-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { toast(e.message, true); }
  });
  document.getElementById("import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch (err) { toast("That file isn't JSON", true); return; }
    const replace = document.getElementById("import-replace").checked;
    if (replace && !confirm("Replace every hub and device setting with the backup?")) return;
    run("import_config", { data, replace }, (r) => `Restored ${r.hubs} hubs and ${r.devices} device settings`).catch(() => {});
  });

  document.getElementById("logout").addEventListener("click", () => window.NO.logout());
  load().catch((e) => toast(e.message, true));
})();
