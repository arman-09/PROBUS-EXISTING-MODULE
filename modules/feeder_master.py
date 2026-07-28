"""
modules/feeder_master.py — Feeder Master Data Store
=====================================================
Stores feeder configuration keyed by AssetCode.
Thread-safe JSON persistence.
"""

import json, os, threading, logging
from datetime import datetime

log = logging.getLogger("feeder_master")


class FeederMaster:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: list[dict] = []
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                log.info(f"Feeder master loaded: {len(self._data)} entries")
            except Exception as e:
                log.warning(f"Feeder master load error: {e}")

    def _save(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    # ─── Lookup by AssetCode ─────────────────────────────
    def lookup(self, asset_code: str) -> dict | None:
        if not asset_code:
            return None
        ac = asset_code.strip().upper()
        with self._lock:
            for f in self._data:
                if (f.get("AssetCode") or "").strip().upper() == ac:
                    return f
        return None

    # ─── CRUD ────────────────────────────────────────────
    def all(self) -> list:
        with self._lock:
            return list(self._data)

    def get_by_idx(self, idx: int) -> dict | None:
        with self._lock:
            return self._data[idx] if 0 <= idx < len(self._data) else None

    def add(self, entry: dict) -> dict:
        entry["updated_at"] = datetime.now().isoformat()
        with self._lock:
            self._data.append(entry)
            self._save()
        log.info(f"Feeder added: {entry.get('AssetCode','?')} — {entry.get('FeederName','?')}")
        return entry

    def update(self, idx: int, entry: dict) -> dict | None:
        entry["updated_at"] = datetime.now().isoformat()
        with self._lock:
            if 0 <= idx < len(self._data):
                self._data[idx] = entry
                self._save()
                return entry
        log.warning(f"Update: index {idx} out of range")
        return None

    def delete(self, idx: int) -> bool:
        with self._lock:
            if 0 <= idx < len(self._data):
                removed = self._data.pop(idx)
                self._save()
                log.info(f"Feeder deleted idx={idx}: {removed.get('FeederName','?')}")
                return True
        return False

    def upsert_by_asset(self, asset_code: str, entry: dict) -> bool:
        """Update if AssetCode exists, else add. Returns True if updated."""
        ac = asset_code.strip().upper()
        with self._lock:
            for i, f in enumerate(self._data):
                if (f.get("AssetCode") or "").strip().upper() == ac:
                    entry["updated_at"] = datetime.now().isoformat()
                    self._data[i] = entry
                    self._save()
                    return True
            entry["updated_at"] = datetime.now().isoformat()
            self._data.append(entry)
            self._save()
            return False

    def import_bulk(self, entries: list) -> dict:
        added, updated = 0, 0
        for e in entries:
            ac = (e.get("AssetCode") or "").strip()
            if ac:
                was_updated = self.upsert_by_asset(ac, e)
                if was_updated:
                    updated += 1
                else:
                    added += 1
            else:
                self.add(e)
                added += 1
        return {"added": added, "updated": updated}

    def __len__(self):
        with self._lock:
            return len(self._data)
