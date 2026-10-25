import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply_update as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundle"
        self.target = self.root / "app"
        (self.bundle / "payload").mkdir(parents=True)
        self.target.mkdir()
        (self.target / "bot.py").write_text("# old bot\n", encoding="utf-8")
        (self.target / "config.yaml").write_text("SECRET_CONFIG", encoding="utf-8")
        self.entries = []

    def add(self, name, body):
        path = self.bundle / "payload" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        self.entries.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        (self.bundle / "manifest.json").write_text(json.dumps({"files": self.entries}), encoding="utf-8")

    def test_dry_run_hashes_and_preserved_config_persona_selection(self):
        self.add("bot.py", "# new bot\n")
        self.add("personas/内在人格.txt", "new persona")
        plan = update.load_plan(self.bundle, self.target)
        self.assertEqual(len(plan), 1)
        self.assertEqual((self.target / "bot.py").read_text(), "# old bot\n")
        backup = update.apply_plan(self.target, plan)
        self.assertEqual((self.target / "config.yaml").read_text(), "SECRET_CONFIG")
        self.assertFalse((self.target / "personas").exists())
        self.assertFalse((self.target / "prompts" / "_current_style.txt").exists())
        self.assertEqual((backup / "files" / "bot.py").read_text(), "# old bot\n")
        update.rollback(self.target, backup)
        self.assertEqual((self.target / "bot.py").read_text(), "# old bot\n")

    def test_opt_in_personas_and_rollback_new_files(self):
        self.add("personas/内在人格.txt", "new persona")
        backup = update.apply_plan(self.target, update.load_plan(self.bundle, self.target, True))
        self.assertEqual((self.target / "personas" / "内在人格.txt").read_text(encoding="utf-8"), "new persona")
        update.rollback(self.target, backup)
        self.assertFalse((self.target / "personas" / "内在人格.txt").exists())

    def test_manifest_rejects_credentials_and_traversal(self):
        for name in ("config.yaml", "conversations.json", "bilibili_account.json", "../bot.py", "prompts/_current_style.txt"):
            (self.bundle / "manifest.json").write_text(json.dumps({"files": [{"path": name, "sha256": "0" * 64}]}))
            with self.subTest(name=name), self.assertRaises(ValueError):
                update.load_plan(self.bundle, self.target)

    def test_tampered_payload_and_wrong_directory_stop_before_changes(self):
        self.add("bot.py", "# new bot\n")
        (self.bundle / "payload" / "bot.py").write_text("# tampered\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            update.load_plan(self.bundle, self.target)
        with self.assertRaisesRegex(ValueError, "actual app"):
            update.load_plan(self.bundle, self.root)
        self.assertEqual((self.target / "bot.py").read_text(), "# old bot\n")

    def test_mid_install_failure_rolls_back_every_changed_file(self):
        self.add("bot.py", "# new bot\n")
        self.add("paper_reader.py", "# new module\n")
        original_copy = update.shutil.copy2
        def copy(source, destination):
            if "paper_reader.py.update-" in str(destination):
                raise OSError("synthetic copy failure")
            return original_copy(source, destination)
        with patch.object(update.shutil, "copy2", side_effect=copy):
            with self.assertRaises(OSError):
                update.apply_plan(self.target, update.load_plan(self.bundle, self.target))
        self.assertEqual((self.target / "bot.py").read_text(), "# old bot\n")
        self.assertFalse((self.target / "paper_reader.py").exists())

    def test_rollback_does_not_destroy_later_edits(self):
        self.add("bot.py", "# new bot\n")
        backup = update.apply_plan(self.target, update.load_plan(self.bundle, self.target))
        (self.target / "bot.py").write_text("# later edit\n")
        with self.assertRaisesRegex(ValueError, "changed after"):
            update.rollback(self.target, backup)
        self.assertEqual((self.target / "bot.py").read_text(), "# later edit\n")


if __name__ == "__main__":
    unittest.main()
