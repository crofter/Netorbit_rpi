/* NetOrbit card for Home Assistant
 * Draws a live radial map from the NetOrbit integration's sensors.
 * https://github.com/the-scott-davis/netorbit  (MIT)
 */
const NETORBIT_VERSION = "0.2.2";

const NO_STATUS = {
  online: { color: "#34d399", label: "Online" },
  recent: { color: "#fbbf24", label: "Seen recently" },
  stale: { color: "#f87171", label: "Not seen" },
  new: { color: "#c084fc", label: "New" },
  unknown: { color: "#64748b", label: "Unknown" },
};

// Possible hubs get a color of their own, so they never look like "seen recently".
const NO_SUGGEST = { color: "#f472b6", light: "#f9a8d4", soft: "rgba(244,114,182,.5)" };

const NO_HUB_TYPE = {
  router: "Router",
  switch: "Switch",
  access_point: "Access point",
  mesh_node: "Mesh node",
  other: "Hub",
};

function noEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function noRel(iso) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "never";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

function noTrunc(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Pure layout: returns positions for hubs and leaves. Exported for tests. */
function netorbitLayout(model, opts) {
  const o = Object.assign({ cx: 500, cy: 500, rDev: 370, rHub1: 135, hubStep: 125, rHubMax: 290, rGroup: 245 }, opts || {});
  const { hubs, root, devices } = model;
  const kidsHub = {};
  const kidsDev = {};
  Object.keys(hubs).forEach((k) => { kidsHub[k] = []; kidsDev[k] = []; });
  Object.values(hubs).forEach((h) => {
    if (h.key === root) return;
    const p = hubs[h.parent] ? h.parent : root;
    kidsHub[p].push(h.key);
  });
  devices.forEach((d) => {
    const p = hubs[d.link] ? d.link : root;
    kidsDev[p].push(d);
  });
  const byLabel = (a, b) => String(a.label).localeCompare(String(b.label));
  Object.values(kidsDev).forEach((a) => a.sort(byLabel));
  Object.values(kidsHub).forEach((a) => a.sort((x, y) => {
    // keep the "new devices" bubble last
    if (x === "__new") return 1;
    if (y === "__new") return -1;
    return String(hubs[x].label).localeCompare(String(hubs[y].label));
  }));

  // DFS producing slots
  const slots = []; // {type:'dev', d, hub} or {type:'gap', hub}
  const range = {};
  const depth = {};
  const guard = new Set();
  const walk = (h, dep) => {
    if (guard.has(h)) return;
    guard.add(h);
    depth[h] = dep;
    const start = slots.length;
    kidsDev[h].forEach((d) => slots.push({ type: "dev", d, hub: h }));
    kidsHub[h].forEach((c) => walk(c, dep + 1));
    if (slots.length === start && h !== root) slots.push({ type: "gap", hub: h });
    range[h] = [start, slots.length - 1];
  };
  walk(root, 0);
  // orphans (cycles) hang off root
  Object.keys(hubs).forEach((h) => { if (!guard.has(h)) { kidsHub[root].push(h); walk(h, 1); } });

  const N = Math.max(slots.length, 1);
  const step = (Math.PI * 2) / N;
  const start = -Math.PI / 2 - (N > 1 ? step * (range[root] ? 0 : 0) : 0);
  const angle = (i) => start + step * i + step / 2;
  const pos = {};
  pos[root] = { x: o.cx, y: o.cy, a: -Math.PI / 2, depth: 0 };
  const stagger = N > 64;
  slots.forEach((s, i) => {
    if (s.type !== "dev") return;
    const a = angle(i);
    const r = o.rDev - (stagger && i % 2 ? 36 : 0);
    pos[s.d.key] = { x: o.cx + r * Math.cos(a), y: o.cy + r * Math.sin(a), a, r, hub: s.hub };
  });
  const maxDepth = Math.max(1, ...Object.keys(depth).map((k) => depth[k]));
  const hubStep = Math.min(o.hubStep, (o.rHubMax - o.rHub1) / Math.max(1, maxDepth - 1));
  Object.keys(hubs).forEach((h) => {
    if (h === root || !range[h]) return;
    const [a0, a1] = range[h];
    const a = (angle(a0) + angle(a1)) / 2;
    const r = hubs[h].kind === "group" ? o.rGroup : Math.min(o.rHubMax, o.rHub1 + (depth[h] - 1) * hubStep);
    pos[h] = { x: o.cx + r * Math.cos(a), y: o.cy + r * Math.sin(a), a, r, depth: depth[h] };
  });
  netorbitSpreadHubs(pos, Object.keys(hubs).filter((h) => h !== root && pos[h]), o);
  return { pos, slots, N, arc: (Math.PI * 2 * o.rDev) / N, kidsHub, depth };
}

/** Push apart hubs that ended up on top of each other, keeping them between the router and the device ring. */
function netorbitSpreadHubs(pos, keys, o) {
  const minD = 88;
  const rMin = 105, rMax = o.rDev - 60;
  for (let iter = 0; iter < 120; iter++) {
    let moved = false;
    for (let i = 0; i < keys.length; i++) {
      for (let j = i + 1; j < keys.length; j++) {
        const p = pos[keys[i]], q = pos[keys[j]];
        const d = Math.hypot(p.x - q.x, p.y - q.y);
        if (d >= minD) continue;
        moved = true;
        if (Math.abs(p.r - q.r) < 30) {
          // Same ring: open up the angle between them.
          let da = q.a - p.a;
          while (da > Math.PI) da -= 2 * Math.PI;
          while (da < -Math.PI) da += 2 * Math.PI;
          const need = (minD - d) / Math.max(p.r, 60) / 2 + 0.01;
          const s = da >= 0 ? 1 : -1;
          p.a -= s * need; q.a += s * need;
          // On a crowded ring, also step one of them outward.
          if (iter > 40) q.r = Math.min(rMax, q.r + 6);
        } else {
          const s = p.r < q.r ? -1 : 1;
          p.r = Math.max(rMin, Math.min(rMax, p.r + s * 4));
          q.r = Math.max(rMin, Math.min(rMax, q.r - s * 4));
        }
        for (const z of [p, q]) { z.x = o.cx + z.r * Math.cos(z.a); z.y = o.cy + z.r * Math.sin(z.a); }
      }
    }
    if (!moved) break;
  }
}

const NO_PLACEMENT = {
  manual: "You placed it",
  controller: "Reported by your Wi-Fi system",
  snmp: "Seen in a switch's MAC table",
  none: "Not known yet",
};

// Groups used when nothing reports which hub a device uses.
const NO_GROUPS = [
  { id: "media", label: "TV & media", icon: "mdi:television-classic", match: /television|speaker|soundbar|gamepad|nintendo|playstation|xbox|virtual-reality|apple|cast|music|movie|play/ },
  { id: "computers", label: "Computers & phones", icon: "mdi:laptop", match: /laptop|monitor|desktop|cellphone|tablet|watch|server|printer|nas|book/ },
  { id: "cameras", label: "Cameras & security", icon: "mdi:cctv", match: /cctv|camera|doorbell|smoke|alarm|lock|shield/ },
  { id: "home", label: "Smart home", icon: "mdi:home-automation", match: /chip|thermostat|robot|garage|light|bulb|power|socket|plug|home|fan|purifier|kettle|fridge|sprinkler|pool|ev-|car|bowl|cat|dog|weather|blind|switch/ },
];

function noGroupFor(icon) {
  const s = String(icon || "");
  for (const g of NO_GROUPS) if (g.match.test(s)) return g.id;
  return "other";
}

class NetOrbitCard extends HTMLElement {
  static getStubConfig() {
    return { title: "Network" };
  }

  setConfig(config) {
    this._config = Object.assign(
      { title: "Network", height: 900, show_ip: false, show_legend: true, hide_sleepy_offline: false, group_by_type: true },
      config || {}
    );
    this._sig = "";
    this._edit = false;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  getCardSize() { return 12; }
  getGridOptions() { return { columns: "full", rows: "auto", min_rows: 8 }; }

  set hass(hass) {
    this._hass = hass;
    if (this._drag || this._popOpen) return; // never redraw under the user's finger
    const model = this._model();
    const sig = JSON.stringify([model.sig, this._edit]);
    if (sig === this._sig) return;
    this._sig = sig;
    this._render(model);
  }

  _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  _entities() {
    const hass = this._hass;
    const cfg = this._config;
    let ids = Object.keys(hass.states).filter((e) => e.startsWith("sensor.") && hass.states[e].attributes.netorbit === true);
    if (cfg.entities) ids = ids.filter((e) => cfg.entities.includes(e));
    if (cfg.device_id && hass.entities) ids = ids.filter((e) => (hass.entities[e] || {}).device_id === cfg.device_id);
    return ids.map((e) => hass.states[e]);
  }

  _model() {
    const hass = this._hass;
    const cfg = this._config;
    const states = this._entities();
    const hubs = {};
    const devices = [];
    let summary = null;
    let root = null;
    const sig = [];
    states.forEach((s) => {
      const a = s.attributes;
      if (a.kind === "summary") {
        summary = s;
        sig.push(JSON.stringify([a.new_devices, a.suggested_hubs, a.group_by_type]));
        return;
      }
      const reg = (hass.entities || {})[s.entity_id] || {};
      const icon = reg.icon || a.icon || "mdi:lan-connect";
      const n = {
        // A name set in Home Assistant's entity settings wins over NetOrbit's own name.
        key: a.node_key, entity: s.entity_id, label: reg.name || a.label || a.friendly_name, ip: a.ip, mac: a.mac, ip_changes: a.ip_changes || 0,
        kind: a.kind, link: a.link, parent: a.link, sleepy: a.sleepy === true, last_seen: a.last_seen,
        hub_type: a.hub_type, hostname: a.hostname, vendor: a.vendor, placement: a.placement, icon,
        status: NO_STATUS[s.state] ? s.state : "unknown",
      };
      sig.push([n.key, n.status, n.link, n.label, icon, n.sleepy ? 1 : 0, n.placement].join("~"));
      if (a.kind === "hub") {
        hubs[n.key] = n;
        if (!n.link && !root) root = n.key;
      } else if (!(cfg.hide_sleepy_offline && n.sleepy && n.status !== "online")) {
        devices.push(n);
      }
    });
    if (!root && Object.keys(hubs).length) root = Object.keys(hubs)[0];
    if (!root) return { hubs, devices, root, summary, sig: sig.join("|"), suggestions: {} };

    // Suggested hubs: mark the device if we track it, otherwise show it in the "new" bubble.
    const suggestions = {};
    ((summary && summary.attributes.suggested_hubs) || []).forEach((s) => (suggestions[String(s.mac || "").toUpperCase()] = s));
    devices.forEach((d) => { if (d.mac && suggestions[d.mac]) d.suggested = suggestions[d.mac]; });

    const fresh = summary ? summary.attributes.new_devices || [] : [];
    const tracked = new Set(devices.map((d) => d.mac));
    const loose = Object.values(suggestions).filter((s) => !tracked.has(String(s.mac).toUpperCase()));
    if ((fresh.length || loose.length) && root) {
      hubs.__new = { key: "__new", label: "New devices", kind: "newhub", parent: root, status: "new", icon: "mdi:tag-plus-outline" };
      fresh.forEach((d) => devices.push({
        key: "new:" + d.key, raw: d, label: d.name || d.mac || d.ip, ip: d.ip, mac: d.mac, kind: "new", link: "__new",
        status: "new", icon: "mdi:help-circle-outline", last_seen: d.last_seen, vendor: d.vendor,
      }));
      loose.forEach((s) => devices.push({
        key: "sug:" + s.key, raw: s, label: s.name, ip: s.ip, mac: s.mac, kind: "new", link: "__new",
        status: "new", icon: "mdi:access-point-network", suggested: s,
      }));
    }

    // Group devices by type when nothing knows where they connect.
    const wantGroups = cfg.group_by_type !== false && (!summary || summary.attributes.group_by_type !== false);
    const loners = devices.filter((d) => d.kind === "device" && d.link === root && d.placement !== "manual");
    if (wantGroups && loners.length >= 8) {
      const byGroup = {};
      loners.forEach((d) => (byGroup[noGroupFor(d.icon)] = (byGroup[noGroupFor(d.icon)] || []).concat(d)));
      // Single devices stay on the router; a group of one is just noise.
      Object.keys(byGroup).forEach((gid) => { if (byGroup[gid].length < 2) delete byGroup[gid]; });
      if (Object.keys(byGroup).length > 1) {
        Object.entries(byGroup).forEach(([gid, list]) => {
          const g = NO_GROUPS.find((x) => x.id === gid) || { label: "Other", icon: "mdi:devices" };
          const key = "__grp_" + gid;
          hubs[key] = { key, label: g.label, kind: "group", parent: root, status: "group", icon: g.icon, count: list.length };
          list.forEach((d) => (d.link = key));
        });
      }
    }
    // Very large networks: keep problem devices visible, fold the rest into "+N more" per hub.
    const max = Number(cfg.max_devices) || 80;
    if (devices.length > max) {
      const rank = { stale: 0, recent: 1, new: 2, unknown: 3, online: 4 };
      const byHub = {};
      devices.forEach((d) => (byHub[d.link] = (byHub[d.link] || []).concat(d)));
      const share = Math.max(3, Math.floor(max / Object.keys(byHub).length));
      const kept = [];
      Object.entries(byHub).forEach(([hub, list]) => {
        // Possible hubs first (they need a tap), then problems, then the rest.
        list.sort((a, b) => (!!b.suggested - !!a.suggested) || (rank[a.status] - rank[b.status]) || String(a.label).localeCompare(String(b.label)));
        if (list.length <= share) { kept.push(...list); return; }
        const shown = list.slice(0, share - 1);
        const rest = list.slice(share - 1);
        kept.push(...shown, {
          key: "more:" + hub, kind: "more", link: hub, label: `+${rest.length} more`, status: "online",
          icon: "mdi:dots-horizontal-circle-outline", members: rest,
        });
      });
      devices.length = 0;
      devices.push(...kept);
    }
    return { hubs, devices, root, summary, suggestions, sig: sig.join("|") };
  }

  _render(model) {
    const cfg = this._config;
    const { hubs, devices, root, summary } = model;
    if (!root) {
      this.shadowRoot.innerHTML = `<ha-card header="${noEsc(cfg.title)}"><div style="padding:16px">No NetOrbit sensors found. Add the NetOrbit integration under Settings, Devices &amp; services.</div></ha-card>`;
      return;
    }
    this._model_ = model;
    const L = netorbitLayout(model, {});
    const pos = L.pos;
    this._pos = pos;
    const C = pos[root];
    const col = (s) => (s === "group" ? "#64748b" : (NO_STATUS[s] || NO_STATUS.unknown).color);
    const live = (s) => s === "online";
    const fs = Math.max(9, Math.min(12.5, L.arc * 0.42));
    const maxChars = L.N > 60 ? 16 : 22;
    const edit = this._edit;
    const admin = this._isAdmin();

    const curve = (p, q, bend) => {
      const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
      const k = bend == null ? 0.18 : bend;
      const cx = mx + (C.x - mx) * k, cy = my + (C.y - my) * k;
      return `M${p.x.toFixed(1)},${p.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${q.x.toFixed(1)},${q.y.toFixed(1)}`;
    };
    let links = "";
    Object.values(hubs).forEach((h) => {
      if (h.key === root || !pos[h.key]) return;
      const p = pos[hubs[h.parent] ? h.parent : root];
      const s = h.kind === "newhub" ? "new" : h.status;
      const faint = h.kind === "group" ? "faint" : "strong";
      links += `<path class="lk ${faint} ${live(s) ? "live" : ""} ${h.kind === "newhub" ? "dashed" : ""}" d="${curve(p, pos[h.key], 0)}" stroke="${col(s)}"/>`;
    });
    devices.forEach((d) => {
      const p = pos[d.key];
      if (!p || !pos[p.hub]) return;
      links += `<path class="lk ${live(d.status) ? "live" : ""} ${d.kind === "new" ? "dashed" : ""} ${d.suggested ? "sug" : ""}" d="${curve(pos[p.hub], p)}" stroke="${d.suggested ? NO_SUGGEST.color : col(d.status)}"/>`;
    });

    const iconFO = (p, r, icon, color) =>
      `<foreignObject x="${(p.x - r).toFixed(1)}" y="${(p.y - r).toFixed(1)}" width="${2 * r}" height="${2 * r}" pointer-events="none"><div xmlns="http://www.w3.org/1999/xhtml" class="ic" style="width:${2 * r}px;height:${2 * r}px;color:${color};--mdc-icon-size:${Math.round(r * 1.05)}px"><ha-icon icon="${noEsc(icon)}"></ha-icon></div></foreignObject>`;

    let gfx = "";
    let labels = "";
    // Text boxes of hub labels placed so far; the router's label always sits right below it.
    const placedLabels = [{ x0: C.x - 45, y0: C.y + 32 + 5, x1: C.x + 45, y1: C.y + 32 + 30 }];
    Object.values(hubs).forEach((h) => {
      const p = pos[h.key];
      if (!p) return;
      const isRoot = h.key === root;
      const isNew = h.kind === "newhub";
      const isGroup = h.kind === "group";
      const r = isRoot ? 32 : isGroup ? 19 : 25;
      const c = col(isNew ? "new" : h.status);
      let sub = isNew
        ? `${devices.filter((d) => d.kind === "new").length} waiting`
        : isGroup ? `${h.count} devices`
        : (cfg.show_ip && h.ip) ? h.ip : (NO_HUB_TYPE[h.hub_type] || "");
      if (String(sub).toLowerCase() === String(h.label).toLowerCase()) sub = "";
      // Label below, above, right or left: the first side whose text box doesn't touch another
      // label, a hub or a nearby device. Otherwise the side that overlaps least.
      const shown = noTrunc(h.label, 26);
      const w = Math.max(String(shown).length * (isGroup ? 7.4 : 8.6), String(sub).length * 6.8) + 10;
      const hgt = sub ? 38 : 22;
      const box = (s) => {
        const x0 = s.anchor === "middle" ? s.x - w / 2 : s.anchor === "start" ? s.x : s.x - w;
        return { x0, y0: s.y1 - 16, x1: x0 + w, y1: s.y1 - 16 + hgt };
      };
      const overlap = (a, b) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
      const square = (q, rr) => ({ x0: q.x - rr, y0: q.y - rr, x1: q.x + rr, y1: q.y + rr });
      const discs = Object.values(hubs).filter((o) => o.key !== h.key && pos[o.key])
        .map((o) => square(pos[o.key], (o.key === root ? 32 : o.kind === "group" ? 19 : 25) + 6));
      const near = devices.map((d) => pos[d.key]).filter((q) => q && Math.abs(q.x - p.x) < 220 && Math.abs(q.y - p.y) < 120).map((q) => square(q, 19));
      const cost = (s) => {
        const b = box(s);
        return placedLabels.reduce((n, o) => n + 4 * overlap(b, o), 0)
          + discs.reduce((n, o) => n + 3 * overlap(b, o), 0)
          + near.reduce((n, o) => n + overlap(b, o), 0);
      };
      const sides = [
        { side: "below", x: p.x, y1: p.y + r + 21, anchor: "middle" },
        { side: "above", x: p.x, y1: p.y - r - 27, anchor: "middle" },
        { side: "right", x: p.x + r + 10, y1: p.y - 2, anchor: "start" },
        { side: "left", x: p.x - r - 10, y1: p.y - 2, anchor: "end" },
      ];
      if (!(isRoot || p.y >= C.y - 4)) sides.unshift(sides.splice(1, 1)[0]);
      const pick = isRoot ? sides[0] : (sides.find((s) => cost(s) === 0) || sides.reduce((a, s) => (cost(s) < cost(a) ? s : a)));
      if (!isRoot) placedLabels.push(box(pick));
      const ly1 = pick.y1;
      const ly2 = ly1 + 16;
      const lx = pick.x;
      const anchor = pick.anchor;
      const target = edit && h.kind === "hub" ? "target" : "";
      gfx += `<g class="node hub ${isGroup ? "group" : ""} ${target}" data-key="${noEsc(h.key)}">` +
        (isGroup ? "" : `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r + 9}" class="halo" fill="${c}"/>`) +
        `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r}" class="disc ${isNew || isGroup ? "dashed" : ""} ${h.status === "stale" ? "pulse" : ""}" stroke="${c}"/>` +
        iconFO(p, r, h.icon, c) + `</g>`;
      // Hub labels go in their own layer, drawn after devices so nothing covers them.
      labels += `<g class="hlabels" pointer-events="none">` +
        `<text x="${lx.toFixed(1)}" y="${ly1.toFixed(1)}" class="hlab ${isGroup ? "glab" : ""}" style="text-anchor:${anchor}">${noEsc(noTrunc(h.label, 26))}</text>` +
        (sub ? `<text x="${lx.toFixed(1)}" y="${ly2.toFixed(1)}" class="hsub" style="text-anchor:${anchor}">${noEsc(sub)}</text>` : "") +
        `</g>`;
    });

    devices.forEach((d) => {
      const p = pos[d.key];
      if (!p) return;
      const c = d.suggested ? NO_SUGGEST.color : col(d.status);
      const deg = (p.a * 180) / Math.PI;
      const nd = ((deg % 360) + 360) % 360;
      const flip = nd > 90 && nd < 270;
      const tr = p.r + 24;
      const tx = C.x + tr * Math.cos(p.a), ty = C.y + tr * Math.sin(p.a);
      const rot = flip ? deg + 180 : deg;
      gfx += `<g class="node dev ${d.status} ${d.sleepy ? "sleepy" : ""} ${edit && d.kind === "device" ? "draggable" : ""}" data-key="${noEsc(d.key)}">` +
        (live(d.status) ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="21" class="halo" fill="${c}"/>` : "") +
        (d.suggested ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="21" class="sugring"/>` : "") +
        `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="15" class="disc ${d.status === "stale" ? "pulse" : ""} ${d.kind === "new" ? "dashed" : ""}" stroke="${c}"/>` +
        iconFO(p, 15, d.icon, c) +
        (d.sleepy ? `<circle cx="${(p.x + 11).toFixed(1)}" cy="${(p.y - 11).toFixed(1)}" r="5" class="zz"/>` : "") +
        (d.placement === "manual" && edit ? `<circle cx="${(p.x - 11).toFixed(1)}" cy="${(p.y - 11).toFixed(1)}" r="4.5" class="pin"/>` : "") +
        `<text x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" transform="rotate(${rot.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)})" text-anchor="${flip ? "end" : "start"}" dominant-baseline="middle" class="dlab" style="font-size:${fs.toFixed(1)}px${d.suggested ? `;fill:${NO_SUGGEST.light}` : ""}">${noEsc(noTrunc(d.label, maxChars))}</text></g>`;
    });

    const counts = { online: 0, recent: 0, stale: 0, new: 0 };
    devices.forEach((d) => {
      if (d.kind === "more") { d.members.forEach((m) => { if (counts[m.status] !== undefined) counts[m.status]++; }); return; }
      if (counts[d.status] !== undefined && !d.key.startsWith("sug:")) counts[d.status]++;
    });
    const nSug = Object.keys(model.suggestions || {}).length;
    const chip = (k) => `<span class="chip"><i style="background:${NO_STATUS[k].color}"></i>${counts[k]} ${NO_STATUS[k].label}</span>`;
    const staleH = summary ? summary.attributes.stale_hours : 24;
    const grouped = Object.values(hubs).some((h) => h.kind === "group");

    this.shadowRoot.innerHTML = `
<style>
  :host { display:block; }
  ha-card { font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif); overflow:hidden; background: radial-gradient(ellipse at 50% 48%, #13213a 0%, #0b1220 55%, #070b14 100%); color:#e2e8f0; border:1px solid rgba(148,163,184,.12); }
  .head { display:flex; flex-wrap:wrap; align-items:center; gap:10px 12px; padding:16px 18px 4px; }
  .title { font-size:20px; font-weight:600; margin-right:auto; display:flex; align-items:center; gap:8px; }
  .title ha-icon { --mdc-icon-size:22px; color:#60a5fa; }
  .chip { display:inline-flex; align-items:center; gap:6px; font-size:12.5px; color:#cbd5e1; background:rgba(148,163,184,.08); border:1px solid rgba(148,163,184,.14); padding:4px 10px; border-radius:999px; font-variant-numeric:tabular-nums; }
  .chip i { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .chip.sug { border-color:rgba(244,114,182,.5); color:#f9a8d4; }
  .editbtn { font:inherit; font-size:12.5px; display:inline-flex; align-items:center; gap:6px; padding:4px 12px; border-radius:999px; border:1px solid rgba(96,165,250,.45); background:transparent; color:#bfdbfe; cursor:pointer; }
  .editbtn.on { background:#2563eb; border-color:#2563eb; color:#fff; }
  .editbtn ha-icon { --mdc-icon-size:16px; }
  .banner { margin:6px 18px 0; padding:8px 12px; border-radius:8px; background:rgba(37,99,235,.15); border:1px solid rgba(96,165,250,.35); font-size:13px; color:#dbeafe; }
  .wrap { position:relative; touch-action:${edit ? "none" : "auto"}; }
  svg { font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif); width:100%; height:auto; display:block; max-height:${Number(cfg.height) || 900}px; user-select:none; -webkit-user-select:none; }
  .ring { fill:none; stroke:rgba(148,163,184,.07); stroke-width:1; }
  .lk { fill:none; stroke-width:1.4; opacity:.45; }
  .lk.strong { stroke-width:2.6; opacity:.75; }
  .lk.faint { stroke-width:1.2; opacity:.25; stroke-dasharray:2 5; }
  .lk.dashed { stroke-dasharray:3 6; }
  .lk.live { stroke-dasharray:6 10; animation: flow 1.6s linear infinite; opacity:.8; }
  .lk.strong.live { stroke-dasharray:10 8; }
  @keyframes flow { to { stroke-dashoffset:-32; } }
  .disc { fill:#0f172a; stroke-width:2.2; }
  .disc.dashed { stroke-dasharray:4 4; }
  .halo { opacity:.13; filter:url(#noblur); }
  .sugring { fill:none; stroke:#f472b6; stroke-width:2; stroke-dasharray:3 3; animation: spin 6s linear infinite; transform-box:fill-box; transform-origin:center; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .pin { fill:#60a5fa; stroke:#0f172a; stroke-width:1.5; }
  .pulse { animation: pulse 1.8s ease-in-out infinite; }
  @keyframes pulse { 50% { stroke-width:5; } }
  .ic { display:flex; align-items:center; justify-content:center; }
  .hlab { fill:#f1f5f9; font-size:15px; font-weight:600; text-anchor:middle; paint-order:stroke; stroke:#0b1220; stroke-width:4px; }
  .hlab.glab { fill:#cbd5e1; font-size:13px; font-weight:500; }
  .hsub { fill:#94a3b8; font-size:12px; text-anchor:middle; paint-order:stroke; stroke:#0b1220; stroke-width:4px; }
  .dlab { fill:#cbd5e1; }
  .dev.recent .dlab { fill:#fde68a; }
  .dev.stale .dlab { fill:#fecaca; }
  .dev.new .dlab { fill:#e9d5ff; }
  .dev.sleepy:not(.online) { opacity:.7; }
  .zz { fill:#a78bfa; stroke:#0f172a; stroke-width:1.5; }
  .node { cursor:pointer; }
  .node.draggable { cursor:grab; }
  .node:hover .disc { stroke-width:4; }
  .node:hover .dlab { fill:#fff; font-weight:600; }
  .node.hub.hot .disc { stroke:#60a5fa !important; stroke-width:6; }
  .ghost { fill:rgba(96,165,250,.25); stroke:#60a5fa; stroke-width:2; pointer-events:none; }
  .tip { position:absolute; pointer-events:none; background:rgba(15,23,42,.96); border:1px solid rgba(148,163,184,.25); border-radius:10px; padding:10px 12px; font-size:12.5px; line-height:1.55; color:#e2e8f0; min-width:190px; max-width:280px; box-shadow:0 10px 30px rgba(0,0,0,.45); opacity:0; transition:opacity .12s; z-index:2; }
  .tiptip { margin-top:6px; padding-top:6px; border-top:1px solid rgba(148,163,184,.2); color:#93c5fd; }
  .tip b { font-size:13.5px; display:block; margin-bottom:2px; }
  .tip .k { color:#94a3b8; display:inline-block; width:74px; }
  .pop { position:absolute; background:rgba(15,23,42,.98); border:1px solid rgba(96,165,250,.45); border-radius:12px; padding:12px; font-size:13px; color:#e2e8f0; z-index:3; width:250px; box-shadow:0 10px 30px rgba(0,0,0,.5); }
  .pop b { display:block; font-size:14px; }
  .pop .muted { color:#94a3b8; font-size:12px; margin-top:2px; }
  .pop .row { display:flex; gap:8px; margin-top:10px; }
  .pop label { display:block; color:#94a3b8; font-size:12px; margin-top:10px; }
  .pop select, .pop input { width:100%; box-sizing:border-box; margin-top:4px; font:inherit; padding:6px 8px; border-radius:8px; border:1px solid rgba(148,163,184,.3); background:#0b1220; color:#e2e8f0; }
  .pop button { flex:1; font:inherit; padding:7px 10px; border-radius:8px; border:1px solid rgba(148,163,184,.3); background:#1e293b; color:#e2e8f0; cursor:pointer; }
  .pop button.go { background:#2563eb; border-color:#2563eb; }
  .pop button.warn { border-color:rgba(248,113,113,.5); color:#fecaca; }
  .mlist { max-height:240px; overflow:auto; margin-top:8px; }
  .mrow { display:flex; align-items:center; gap:8px; padding:4px 2px; cursor:pointer; border-radius:6px; }
  .mrow:hover { background:rgba(148,163,184,.1); }
  .mrow i { width:8px; height:8px; border-radius:50%; flex:none; }
  .mrow span { margin-left:auto; color:#94a3b8; font-size:12px; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; padding:2px 18px 14px; font-size:12px; color:#94a3b8; }
  .legend span { display:inline-flex; align-items:center; gap:6px; }
  .legend .dot { width:9px; height:9px; border-radius:50%; display:inline-block; }
  @media (prefers-reduced-motion: reduce) { .lk.live, .pulse, .sugring { animation:none; } }
</style>
<ha-card>
  <div class="head">
    <div class="title"><ha-icon icon="mdi:orbit"></ha-icon>${noEsc(cfg.title)}</div>
    ${chip("online")}${chip("recent")}${chip("stale")}${counts.new ? chip("new") : ""}
    ${nSug ? `<span class="chip sug"><i style="background:${NO_SUGGEST.color}"></i>${nSug} possible hub${nSug > 1 ? "s" : ""}</span>` : ""}
    ${admin ? `<button class="editbtn ${edit ? "on" : ""}"><ha-icon icon="${edit ? "mdi:check" : "mdi:pencil"}"></ha-icon>${edit ? "Done" : "Edit layout"}</button>` : ""}
  </div>
  ${edit ? `<div class="banner">Drag a device onto the hub it connects through, or tap it for more options. Tap a hub to rename or remove it.</div>` : ""}
  <div class="wrap">
    <svg class="map" viewBox="-70 -40 1140 1090" role="img" aria-label="Network map">
      <defs><filter id="noblur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter></defs>
      <circle class="ring" cx="${C.x}" cy="${C.y}" r="370"/>
      ${links}
      ${gfx}${labels}
      <circle class="ghost" r="17" cx="-500" cy="-500"/>
    </svg>
    <div class="tip"></div>
  </div>
  ${cfg.show_legend ? `<div class="legend">
    <span>Moving line = answering pings</span>
    <span><i class="dot" style="background:${NO_STATUS.recent.color}"></i>Seen in the last ${noEsc(staleH)} h</span>
    <span><i class="dot" style="background:${NO_STATUS.stale.color}"></i>Not seen for ${noEsc(staleH)} h+</span>
    <span><i class="dot" style="background:#a78bfa"></i>Allowed to sleep</span>
    ${nSug ? `<span><i class="dot" style="background:${NO_SUGGEST.color}"></i>Might be a hub, tap to confirm</span>` : ""}
    ${grouped ? `<span>Grouped by type until NetOrbit knows where they connect</span>` : ""}
  </div>` : ""}
</ha-card>`;

    const btn = this.shadowRoot.querySelector(".editbtn");
    if (btn) btn.addEventListener("click", () => { this._edit = !this._edit; this._sig = ""; this.hass = this._hass; });
    this._wire(model);
  }

  _wire(model) {
    const { hubs, devices } = model;
    const all = {};
    Object.values(hubs).forEach((h) => (all[h.key] = h));
    devices.forEach((d) => (all[d.key] = d));
    const root = model.root;
    const col = (s) => (s === "group" ? "#64748b" : (NO_STATUS[s] || NO_STATUS.unknown).color);
    const tip = this.shadowRoot.querySelector(".tip");
    const wrap = this.shadowRoot.querySelector(".wrap");
    const svg = this.shadowRoot.querySelector("svg.map");
    const ghost = this.shadowRoot.querySelector(".ghost");
    const place = (el, ev, w, h) => {
      const r = wrap.getBoundingClientRect();
      let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
      if (x + w > r.width) x -= w + 24;
      if (y + h > r.height) y -= h + 20;
      el.style.left = Math.max(4, x) + "px"; el.style.top = Math.max(4, y) + "px";
    };
    const toSvg = (ev) => {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const m = svg.getScreenCTM();
      return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 };
    };
    const nearestHub = (p) => {
      let best = null, bestD = 75;
      Object.values(hubs).forEach((h) => {
        if (h.kind !== "hub") return;
        const hp = this._pos[h.key];
        if (!hp) return;
        const d = Math.hypot(hp.x - p.x, hp.y - p.y);
        if (d < bestD) { best = h; bestD = d; }
      });
      return best;
    };

    this.shadowRoot.querySelectorAll(".node[data-key]").forEach((el) => {
      const n = all[el.dataset.key];
      if (!n) return;
      el.addEventListener("mouseenter", () => {
        if (this._drag) return;
        tip.innerHTML = this._tipHtml(n, hubs, col);
        tip.style.opacity = "1";
      });
      el.addEventListener("mousemove", (ev) => place(tip, ev, 230, 170));
      el.addEventListener("mouseleave", () => (tip.style.opacity = "0"));

      el.addEventListener("pointerdown", (ev) => {
        if (!this._edit || n.kind !== "device") return;
        ev.preventDefault();
        this._drag = { n, x0: ev.clientX, y0: ev.clientY, moved: false, id: ev.pointerId };
        el.setPointerCapture(ev.pointerId);
      });
      el.addEventListener("pointermove", (ev) => {
        const d = this._drag;
        if (!d || d.id !== ev.pointerId) return;
        if (!d.moved && Math.hypot(ev.clientX - d.x0, ev.clientY - d.y0) < 6) return;
        d.moved = true;
        tip.style.opacity = "0";
        const p = toSvg(ev);
        ghost.setAttribute("cx", p.x); ghost.setAttribute("cy", p.y);
        const h = nearestHub(p);
        this.shadowRoot.querySelectorAll(".node.hub.hot").forEach((x) => x.classList.remove("hot"));
        if (h) { const hel = this.shadowRoot.querySelector(`.node.hub[data-key="${CSS.escape(h.key)}"]`); if (hel) hel.classList.add("hot"); }
        d.target = h;
      });
      const finish = (ev) => {
        const d = this._drag;
        if (!d || d.id !== ev.pointerId) return;
        this._drag = null;
        // Judge the drop where the pointer was released. A quick flick can end on a hub
        // without a pointermove there.
        if (ev.type === "pointerup" && Math.hypot(ev.clientX - d.x0, ev.clientY - d.y0) >= 6) {
          d.moved = true;
          d.target = nearestHub(toSvg(ev));
        }
        ghost.setAttribute("cx", -500); ghost.setAttribute("cy", -500);
        this.shadowRoot.querySelectorAll(".node.hub.hot").forEach((x) => x.classList.remove("hot"));
        if (d.moved) {
          if (d.target && d.target.key !== d.n.link) this._call("assign_device", { device: [this._ref(d.n)], hub: d.target.key.replace(/^hub:/, "") });
          else this._sig = "";
          return;
        }
        this._devicePopup(d.n, ev, wrap, place, hubs);
      };
      el.addEventListener("pointerup", finish);
      el.addEventListener("pointercancel", (ev) => { if (this._drag) { this._drag = null; ghost.setAttribute("cx", -500); } });

      el.addEventListener("click", (ev) => {
        if (this._edit && n.kind === "device") return; // handled by pointer events
        if (n.suggested && this._isAdmin()) { this._suggestPopup(n, ev, wrap, place); return; }
        if (n.kind === "new") { this._newPopup(n, ev, wrap, place); return; }
        if (n.kind === "more") { this._morePopup(n, ev, wrap, place); return; }
        if (this._edit && n.kind === "hub") { this._hubPopup(n, ev, wrap, place, n.key === root); return; }
        if (n.entity) this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: n.entity }, bubbles: true, composed: true }));
      });
    });
  }

  _tipHtml(n, hubs, col) {
    if (n.kind === "more") {
      const counts = {};
      n.members.forEach((m) => (counts[m.status] = (counts[m.status] || 0) + 1));
      return `<b>${noEsc(n.label)}</b><div>${Object.entries(counts).map(([s, c]) => `${c} ${(NO_STATUS[s] || NO_STATUS.unknown).label.toLowerCase()}`).join(", ")}</div><div style="color:#94a3b8">Tap to list them. Devices with problems are always shown on the map.</div>`;
    }
    if (n.kind === "newhub") return `<b>New devices</b><div>Found on the network but not tracked yet, plus anything that might be a hub. Tap one to decide.</div>`;
    if (n.kind === "group") return `<b>${noEsc(n.label)}</b><div>NetOrbit does not know which access point or switch these use yet. Use Edit layout, the NetOrbit settings, or an AI assistant to place them.</div>`;
    const via = n.kind === "hub" ? (hubs[n.parent] ? hubs[n.parent].label : "") : (hubs[n.link] && hubs[n.link].kind === "hub" ? hubs[n.link].label : "");
    return `<b>${noEsc(n.label)}</b>` +
      (n.suggested ? `<div style="color:${NO_SUGGEST.light}">Might be a ${noEsc((n.suggested.type || "hub").replace("_", " "))}: ${noEsc((n.suggested.reasons || []).join(", "))}</div>` : "") +
      `<div><span class="k">Status</span><span style="color:${col(n.status)}">${(NO_STATUS[n.status] || NO_STATUS.unknown).label}</span>${n.sleepy ? " · sleeps" : ""}</div>` +
      (n.ip ? `<div><span class="k">IP</span>${noEsc(n.ip)}</div>` : "") +
      (n.mac ? `<div><span class="k">MAC</span>${noEsc(n.mac)}</div>` : "") +
      (n.vendor ? `<div><span class="k">Maker</span>${noEsc(n.vendor)}</div>` : "") +
      (n.hostname && n.hostname !== n.label ? `<div><span class="k">Host</span>${noEsc(n.hostname)}</div>` : "") +
      (via ? `<div><span class="k">Via</span>${noEsc(via)}</div>` : "") +
      (n.kind === "device" && n.placement ? `<div><span class="k">Placed</span>${noEsc(NO_PLACEMENT[n.placement] || n.placement)}</div>` : "") +
      `<div><span class="k">Last seen</span>${n.status === "online" ? "now" : noRel(n.last_seen)}</div>` +
      this._dhcpTip(n);
  }

  _dhcpTip(n) {
    const c = n.ip_changes || 0;
    if (n.kind === "hub" && c >= 1) {
      return `<div class="tiptip">Its IP has changed ${c} time${c > 1 ? "s" : ""}. NetOrbit follows it by MAC address, but network gear works best with a DHCP reservation in your router.</div>`;
    }
    if (n.kind === "device" && c >= 2) {
      return `<div class="tiptip">Its IP has changed ${c} times. NetOrbit keeps your settings by MAC address, so nothing is lost. A DHCP reservation keeps the address steady if other things rely on it.</div>`;
    }
    return "";
  }

  _ref(n) {
    return (n.raw && (n.raw.mac || n.raw.ip)) || n.mac || n.ip || n.label;
  }

  _call(service, data) {
    const entryId = this._summaryEntry();
    if (entryId) data.config_entry_id = entryId;
    this._sig = "";
    return this._hass.callService("netorbit", service, data).catch((e) => {
      console.error("NetOrbit:", e);
      this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message: `NetOrbit: ${e.message || e}` }, bubbles: true, composed: true }));
    });
  }

  _openPop(html, ev, wrap, place, w = 250, h = 200) {
    this._closePop(wrap);
    const pop = document.createElement("div");
    pop.className = "pop";
    pop.innerHTML = html;
    wrap.appendChild(pop);
    place(pop, ev, w, h);
    this._popOpen = true;
    pop.querySelectorAll(".x").forEach((b) => b.addEventListener("click", () => this._closePop(wrap)));
    return pop;
  }

  _closePop(wrap) {
    const old = wrap.querySelector(".pop");
    if (old) old.remove();
    if (this._popOpen) { this._popOpen = false; this._sig = ""; this.hass = this._hass; }
  }

  _hubOptions(hubs, current) {
    const real = hubs[current] && hubs[current].kind === "hub";
    return (real ? "" : `<option value="" selected disabled>Choose a hub…</option>`) +
      Object.values(hubs).filter((h) => h.kind === "hub")
        .sort((a, b) => String(a.label).localeCompare(String(b.label)))
        .map((h) => `<option value="${noEsc(h.key.replace(/^hub:/, ""))}" ${h.key === current ? "selected" : ""}>${noEsc(h.label)}</option>`).join("");
  }

  _devicePopup(n, ev, wrap, place, hubs) {
    const types = Object.entries(NO_HUB_TYPE).filter(([k]) => k !== "router").map(([k, v]) => `<option value="${k}" ${k === "access_point" ? "selected" : ""}>${v}</option>`).join("");
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(NO_PLACEMENT[n.placement] || "")}</div>` +
      `<label>Connects through<select class="hub">${this._hubOptions(hubs, n.link)}</select></label>` +
      `<div class="row"><button class="go move">Move</button>${n.placement === "manual" ? `<button class="reset">Automatic</button>` : ""}</div>` +
      `<label>Or this device is itself a<select class="type">${types}</select></label>` +
      `<div class="row"><button class="mk">Make it a hub</button><button class="x">Close</button></div>`,
      ev, wrap, place, 250, 260
    );
    const q = (s) => pop.querySelector(s);
    q(".move").addEventListener("click", () => {
      const hub = q(".hub").value;
      if (!hub) { q(".hub").focus(); return; }
      this._closePop(wrap);
      this._call("assign_device", { device: [this._ref(n)], hub });
    });
    if (q(".reset")) q(".reset").addEventListener("click", () => { this._closePop(wrap); this._call("reset_placement", { device: [this._ref(n)] }); });
    q(".mk").addEventListener("click", () => { const t = q(".type").value; this._closePop(wrap); this._call("confirm_hub", { device: this._ref(n), type: t }); });
  }

  _hubPopup(n, ev, wrap, place, isRoot) {
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(NO_HUB_TYPE[n.hub_type] || "Hub")}</div>` +
      `<label>Name<input class="name" value="${noEsc(n.label)}"></label>` +
      `<div class="row"><button class="go save">Save</button>${isRoot ? "" : `<button class="warn rm">Remove</button>`}<button class="x">Close</button></div>`,
      ev, wrap, place, 250, 160
    );
    const q = (s) => pop.querySelector(s);
    q(".save").addEventListener("click", () => {
      const name = q(".name").value.trim();
      this._closePop(wrap);
      if (name && name !== n.label) this._call("update_hub", { hub: n.key.replace(/^hub:/, ""), name });
    });
    if (q(".rm")) q(".rm").addEventListener("click", () => { this._closePop(wrap); this._call("remove_hub", { hub: n.key.replace(/^hub:/, "") }); });
  }

  _suggestPopup(n, ev, wrap, place) {
    const s = n.suggested;
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">NetOrbit thinks this is a ${noEsc((s.type || "hub").replace("_", " "))} because it is ${noEsc((s.reasons || []).join(", "))}.</div>` +
      `<div class="row"><button class="go yes">It's a hub</button><button class="no">Not a hub</button></div><div class="row"><button class="x">Close</button></div>`,
      ev, wrap, place, 250, 170
    );
    const q = (sel) => pop.querySelector(sel);
    q(".yes").addEventListener("click", () => { this._closePop(wrap); this._call("confirm_hub", { device: s.key, type: s.type || "access_point" }); });
    q(".no").addEventListener("click", () => { this._closePop(wrap); this._call("dismiss_hub", { device: s.key }); });
  }

  _newPopup(n, ev, wrap, place) {
    const admin = this._isAdmin();
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(n.ip || "")} ${noEsc(n.vendor || "")}</div>` +
      (admin
        ? `<div class="row"><button class="go track">Track</button><button class="ign">Ignore</button><button class="x">Close</button></div>`
        : `<div class="muted" style="margin-top:8px">An admin can track or ignore this device.</div><div class="row"><button class="x">Close</button></div>`),
      ev, wrap, place, 250, 120
    );
    const q = (s) => pop.querySelector(s);
    if (q(".track")) q(".track").addEventListener("click", () => { this._closePop(wrap); this._call("track_device", { mac: n.raw.mac || undefined, ip: n.raw.ip || undefined }); });
    if (q(".ign")) q(".ign").addEventListener("click", () => { this._closePop(wrap); this._call("ignore_device", { mac: n.raw.mac || undefined, ip: n.raw.ip || undefined }); });
  }

  _morePopup(n, ev, wrap, place) {
    const rows = n.members.map((m) => `<div class="mrow" data-e="${noEsc(m.entity || "")}"><i style="background:${(NO_STATUS[m.status] || NO_STATUS.unknown).color}"></i>${noEsc(m.label)}<span>${noEsc(m.ip || "")}</span></div>`).join("");
    const pop = this._openPop(`<b>${noEsc(n.label)}</b><div class="mlist">${rows}</div><div class="row"><button class="x">Close</button></div>`, ev, wrap, place, 260, 340);
    pop.querySelectorAll(".mrow[data-e]").forEach((r) => r.addEventListener("click", () => {
      if (!r.dataset.e) return;
      this._closePop(wrap);
      this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: r.dataset.e }, bubbles: true, composed: true }));
    }));
  }

  _summaryEntry() {
    const s = this._entities().find((x) => x.attributes.kind === "summary");
    return s ? s.attributes.config_entry_id : undefined;
  }
}

if (!customElements.get("netorbit-card")) customElements.define("netorbit-card", NetOrbitCard);
window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "netorbit-card")) {
  window.customCards.push({
    type: "netorbit-card",
    name: "NetOrbit",
    description: "Live radial map of your network from the NetOrbit integration.",
    preview: false,
    documentationURL: "https://github.com/the-scott-davis/netorbit",
  });
}
if (typeof module !== "undefined") module.exports = { netorbitLayout, noGroupFor };
console.info("%c NETORBIT %c " + NETORBIT_VERSION, "color:#0b1220;background:#34d399;font-weight:700", "color:#34d399");
