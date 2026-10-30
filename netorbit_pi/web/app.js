/* The map page: feeds the original NetOrbit card a Home Assistant-like `hass` object
 * built from /api/state, and keeps it fresh from the /api/events stream. */
(function () {
  const { esc, api, toast, rel, when, statusLine } = window.NO;
  const STATUS = {
    online: { color: "#34d399", label: "Online" },
    recent: { color: "#fbbf24", label: "Seen recently" },
    stale: { color: "#f87171", label: "Not seen" },
    new: { color: "#c084fc", label: "New" },
  };

  const params = new URLSearchParams(location.search);
  const cardConfig = {
    type: "custom:netorbit-card",
    title: params.get("title") || "Home Network",
    height: Number(params.get("height")) || 900,
    show_ip: params.get("show_ip") === "1",
    show_legend: params.get("legend") !== "0",
    hide_sleepy_offline: params.get("hide_sleepy") === "1",
    max_devices: Number(params.get("max")) || 80,
  };

  const host = document.getElementById("map");
  const statusEl = document.getElementById("status");
  const noticeEl = document.getElementById("notice");
  let card = null;
  let hass = null;
  let lastStatus = null;

  function makeHass(payload) {
    const states = {};
    payload.states.forEach((s) => (states[s.entity_id] = s));
    return {
      states,
      user: payload.user,
      entities: {},
      callService: async (domain, service, data) => {
        const res = await api(`/api/services/${encodeURIComponent(service)}`, data || {});
        await refresh();
        return res;
      },
    };
  }

  async function refresh() {
    let payload;
    try {
      payload = await api("/api/state");
    } catch (e) {
      statusLine(statusEl, { configured: true, running: true, ok: false, error: "Can't reach NetOrbit" });
      return;
    }
    lastStatus = payload.status;
    statusLine(statusEl, payload.status);
    document.getElementById("logout").hidden = !payload.status.password_set;
    if (!payload.status.configured) {
      noticeEl.hidden = false;
      noticeEl.className = "notice";
      noticeEl.innerHTML = `NetOrbit couldn't work out your network on its own. <a href="/settings">Set the subnet and router in Settings</a>.`;
    } else if (payload.status.error) {
      noticeEl.hidden = false;
      noticeEl.className = "notice bad";
      noticeEl.textContent = payload.status.error;
    } else if (!payload.status.password_set && !payload.status.demo && !sessionStorageGet("pwhint")) {
      noticeEl.hidden = false;
      noticeEl.className = "notice";
      noticeEl.innerHTML = `Anyone on your network can change this map. <a href="/settings#security">Set an admin password</a> · <a href="#" id="hidehint">Not now</a>`;
      const hide = document.getElementById("hidehint");
      if (hide) hide.onclick = (e) => { e.preventDefault(); sessionStorageSet("pwhint", "1"); noticeEl.hidden = true; };
    } else {
      noticeEl.hidden = true;
    }
    if (!payload.states.length) {
      if (!card) host.innerHTML = `<div class="empty">${payload.status.configured ? "Scanning your network… the first sweep takes up to a minute." : ""}</div>`;
      return;
    }
    hass = makeHass(payload);
    if (!card) {
      host.innerHTML = "";
      card = document.createElement("netorbit-card");
      card.setConfig(cardConfig);
      host.appendChild(card);
    }
    card.hass = hass;
  }

  function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } }

  // Live updates: the server says "update" after every ping round.
  let es = null;
  let debounce = null;
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/events");
    es.addEventListener("update", () => {
      clearTimeout(debounce);
      debounce = setTimeout(refresh, 150);
    });
    es.addEventListener("netorbit", (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.event === "netorbit_new_device") toast(`New device: ${d.name || d.mac || d.ip}`);
      } catch (e) { /* ignore */ }
    });
    es.onerror = () => {
      es.close();
      es = null;
      setTimeout(connect, 5000);
    };
  }

  // Card events that Home Assistant would have handled.
  document.addEventListener("hass-notification", (ev) => toast(ev.detail.message, true));
  document.addEventListener("hass-more-info", (ev) => openHistory(ev.detail.entityId));

  // ----- history drawer (Home Assistant's "more info" dialog) -----
  const drawer = document.getElementById("drawer");
  const dBody = drawer.querySelector(".body");
  const dTitle = drawer.querySelector("h2");
  let dKey = null;
  let dDays = 1;
  drawer.querySelector(".close").addEventListener("click", closeHistory);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHistory(); });

  function closeHistory() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    dKey = null;
  }

  async function openHistory(entityId) {
    const s = hass && hass.states[entityId];
    if (!s) return;
    dKey = s.attributes.node_key;
    dTitle.textContent = s.attributes.label;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    await drawHistory(s);
  }

  async function drawHistory(s) {
    const a = s.attributes;
    const st = STATUS[s.state] || { color: "#64748b", label: s.state };
    let html = `<dl class="kv">
      <dt>Status</dt><dd style="color:${st.color}">${esc(st.label)}${a.sleepy ? " · allowed to sleep" : ""}</dd>
      ${a.ip ? `<dt>IP</dt><dd>${esc(a.ip)}</dd>` : ""}
      ${a.mac ? `<dt>MAC</dt><dd>${esc(a.mac)}</dd>` : ""}
      ${a.vendor ? `<dt>Maker</dt><dd>${esc(a.vendor)}</dd>` : ""}
      ${a.hostname ? `<dt>Host</dt><dd>${esc(a.hostname)}</dd>` : ""}
      <dt>Last seen</dt><dd>${s.state === "online" ? "now" : esc(rel(a.last_seen))}</dd>
      <dt>First seen</dt><dd>${esc(a.first_seen ? when(a.first_seen) : "unknown")}</dd>
    </dl>
    <div class="seg" role="group" aria-label="Period">
      ${[1, 7, 30].map((d) => `<button data-d="${d}" class="${d === dDays ? "on" : ""}">${d === 1 ? "24 hours" : d + " days"}</button>`).join("")}
    </div><div class="hist small">Loading…</div>`;
    dBody.innerHTML = html;
    dBody.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => { dDays = Number(b.dataset.d); drawHistory(s); }));
    let h;
    try {
      h = await api(`/api/history/${encodeURIComponent(a.node_key)}?days=${dDays}`);
    } catch (e) {
      dBody.querySelector(".hist").textContent = e.message;
      return;
    }
    if (dKey !== a.node_key) return;
    const share = h.online_share;
    const pct = share == null ? null : Math.round(share * 1000) / 10;
    const items = h.changes.slice().reverse().map((c) => {
      const cs = STATUS[c.status] || { color: "#64748b", label: c.status };
      return `<li><span class="pill" style="color:${cs.color}">${esc(cs.label)}</span>${c.ip && c.ip !== a.ip ? `<span class="small">${esc(c.ip)}</span>` : ""}<time datetime="${esc(c.at)}">${esc(when(c.at))}</time></li>`;
    }).join("");
    dBody.querySelector(".hist").outerHTML = `
      <div>${pct == null ? "Not enough history yet." : `Online ${pct}% of the time`}</div>
      ${pct == null ? "" : `<div class="bar"><i style="width:${pct}%;background:${STATUS.online.color}"></i><i style="flex:1;background:${STATUS.stale.color};opacity:.55"></i></div>`}
      ${items ? `<ul class="timeline">${items}</ul>` : `<p class="small">No changes in this period.</p>`}`;
  }

  document.getElementById("logout").addEventListener("click", () => window.NO.logout());
  document.getElementById("scan").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    toast("Scanning the whole network…");
    try {
      const r = await api("/api/services/scan_now", {});
      toast(`Scan done: ${r.devices} on the map`);
      await refresh();
    } catch (err) {
      toast(err.message, true);
    } finally {
      b.disabled = false;
    }
  });

  setInterval(() => { if (lastStatus) statusLine(statusEl, lastStatus); }, 30000);
  refresh().then(connect);
})();
