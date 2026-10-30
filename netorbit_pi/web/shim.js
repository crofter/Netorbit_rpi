/* Stand-ins for the two Home Assistant elements the NetOrbit card uses, so the card runs
 * unchanged on its own page.
 *   <ha-card>  a plain block with an optional header
 *   <ha-icon icon="mdi:name">  an inline SVG from the bundled icon set (icons.js)
 */
(function () {
  const ICONS = window.NETORBIT_ICONS || {};
  const FALLBACK = ICONS["help-circle"] || "M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Z";

  class HaCard extends HTMLElement {
    connectedCallback() {
      if (this._done) return;
      this._done = true;
      this.style.display = "block";
      this.style.borderRadius = this.style.borderRadius || "var(--ha-card-border-radius, 16px)";
      const header = this.getAttribute("header");
      if (header) {
        const h = document.createElement("div");
        h.className = "card-header";
        h.style.cssText = "font-size:20px;font-weight:600;padding:16px 16px 0";
        h.textContent = header;
        this.prepend(h);
      }
    }
  }

  class HaIcon extends HTMLElement {
    static get observedAttributes() { return ["icon"]; }
    connectedCallback() { this._draw(); }
    attributeChangedCallback() { this._draw(); }
    set icon(v) { this.setAttribute("icon", v); }
    get icon() { return this.getAttribute("icon"); }
    _draw() {
      const name = String(this.getAttribute("icon") || "").replace(/^mdi:/, "");
      const d = ICONS[name] || FALLBACK;
      this.style.display = "inline-flex";
      this.style.width = this.style.height = "var(--mdc-icon-size, 24px)";
      this.style.verticalAlign = "middle";
      this.innerHTML = `<svg viewBox="0 0 24 24" width="100%" height="100%" aria-hidden="true" focusable="false"><path fill="currentColor" d="${d}"/></svg>`;
    }
  }

  if (!customElements.get("ha-card")) customElements.define("ha-card", HaCard);
  if (!customElements.get("ha-icon")) customElements.define("ha-icon", HaIcon);
})();
