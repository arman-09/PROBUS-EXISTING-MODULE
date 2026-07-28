/**
 * api.js — Backend API Bridge for TPNODL Monitor
 * Replaces all localStorage/sample-data logic when backend is running.
 * Drop this file in /static/ and include in index.html
 */

const API = (function () {
  const BASE = window.location.origin; // same host as Flask

  async function _req(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    try {
      const r = await fetch(BASE + path, opts);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) {
      console.error("API error", path, e);
      return null;
    }
  }

  return {
    // ── Live data ───────────────────────────────────────
    async fetchLive() {
      return _req("GET", "/api/live");
    },
    async triggerFetch() {
      return _req("POST", "/api/fetch");
    },
    async status() {
      return _req("GET", "/api/status");
    },

    // ── Alerts ──────────────────────────────────────────
    async getAlerts(params = {}) {
      const q = new URLSearchParams(params).toString();
      return _req("GET", `/api/alerts${q ? "?" + q : ""}`);
    },
    async ackAlert(id) {
      return _req("POST", `/api/alerts/${id}/ack`);
    },
    async ackAll() {
      return _req("POST", "/api/alerts/ack-all");
    },
    async clearAlerts() {
      return _req("POST", "/api/alerts/clear");
    },

    // ── Feeder Master ───────────────────────────────────
    async getFeeders(params = {}) {
      const q = new URLSearchParams(params).toString();
      return _req("GET", `/api/feeders${q ? "?" + q : ""}`);
    },
    async addFeeder(entry) {
      return _req("POST", "/api/feeders", entry);
    },
    async updateFeeder(idx, entry) {
      return _req("PUT", `/api/feeders/${idx}`, entry);
    },
    async deleteFeeder(idx) {
      return _req("DELETE", `/api/feeders/${idx}`);
    },
    async importFeeders(entries) {
      return _req("POST", "/api/feeders/import", { entries });
    },

    // ── Config ──────────────────────────────────────────
    async getConfig() {
      return _req("GET", "/api/config");
    },
    async saveConfig(section, data) {
      return _req("POST", `/api/config/${section}`, data);
    },

    // ── Notifications ───────────────────────────────────
    async testEmail() {
      return _req("POST", "/api/email/test");
    },
    async testWhatsApp() {
      return _req("POST", "/api/whatsapp/test");
    },
    async getWALink(number, message) {
      const q = new URLSearchParams({ number, message }).toString();
      return _req("GET", `/api/whatsapp/link?${q}`);
    },

    // ── Logs ─────────────────────────────────────────────
    async getLogs() {
      return _req("GET", "/api/logs");
    },
  };
})();
