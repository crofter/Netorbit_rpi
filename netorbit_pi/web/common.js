/* Helpers shared by the map and settings pages. */
window.NO = (function () {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, body) {
    const opts = body === undefined
      ? { credentials: "same-origin" }
      : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    if (res.status === 401 && !path.startsWith("/api/login")) {
      location.href = "/login";
      throw new Error("Log in first");
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  let toastTimer;
  function toast(msg, bad) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.toggle("bad", !!bad);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), bad ? 6000 : 3500);
  }

  function rel(iso) {
    if (!iso) return "never";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return "never";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  function when(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function statusLine(el, status) {
    if (!el || !status) return;
    el.classList.toggle("ok", !!status.ok);
    el.classList.toggle("bad", status.running && !status.ok);
    const text = !status.configured ? "Not set up yet"
      : status.error ? status.error
      : status.last_scan ? `Last full scan ${rel(status.last_scan)}`
      : "Scanning…";
    el.innerHTML = `<i class="dot"></i><span>${esc(text)}${status.demo ? " · demo network" : ""}</span>`;
  }

  async function logout() {
    await api("/api/logout", {});
    location.href = "/login";
  }

  return { esc, api, toast, rel, when, statusLine, logout };
})();
