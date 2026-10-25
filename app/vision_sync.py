"""Global, reversible vision route overlay; the saved independent route is never rewritten."""
import json
from pathlib import Path


class VisionSync:
    def __init__(self, path):
        self.path = Path(path)
        self.enabled = False
        self.storage_ok = True
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if state.get("version") != 1 or type(state.get("enabled")) is not bool:
                    raise ValueError("Invalid vision sync state")
                self.enabled = state["enabled"]
            except (OSError, ValueError, AttributeError):
                self.storage_ok = False

    def set_enabled(self, enabled):
        if not self.storage_ok:
            raise OSError("Invalid vision sync state; inspect vision_sync_state.json")
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "enabled": bool(enabled)}), encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.path)
        self.enabled = bool(enabled)

    def route(self, config, main=None, force=None):
        original = dict(config.get("qwen_vl", {}) or {})
        if not self.storage_ok:
            return {**original, "enabled": False, "api_key": "", "_sync_error": True}
        synchronized = self.enabled if force is None else force
        if not synchronized:
            return original
        main = main or {}
        return {**original, "enabled": bool(main.get("enabled") and main.get("api_key")),
                "api_key": main.get("api_key", ""), "endpoint": main.get("endpoint", ""),
                "model": main.get("model", ""), "_synced": True}
