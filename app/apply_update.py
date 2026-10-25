"""Offline, explicit-target update installer. Never edits config, secrets, or history."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in (".", "..") or "\\" in p or ":" in p for p in parts):
        raise ValueError("Unsafe relative path")
    dest = root.joinpath(*parts)
    if not dest.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escaped target directory")
    if any(p.is_symlink() for p in (dest, *dest.parents) if p != root.parent):
        raise ValueError("Symlinks are not allowed in update paths")
    return dest


def permitted(relative):
    path = PurePosixPath(relative)
    if len(path.parts) == 1:
        return path.name in {
            "qq_media.py", "media_reader.py", "video_review.py", "VIDEO_REVIEW.md",
            "proactive_images.py", "PROACTIVE_IMAGES.md", "public_message.py", "PUBLIC_MESSAGES.md",
            "bot.py", "image_network.py", "image_cache.py", "paper_download.py", "apply_download_update.py", "DOWNLOAD_RELIABILITY.md", "reading_sessions.py", "reading_pages.py", "reading_bridge.py", "persona.py", "proactive.py", "paper_autonomy.py", "mechanism_assistant.py", "vision_sync.py", "bilibili_feed.py", "bilibili_frames.py",
            "bilibili_account.py", "whale_stickers.py", "paper_reader.py", "group_file_reader.py", "model_decision.py", "file_translation.py",
            "image_search.py", "wiki_lookup.py", "switch_style.py", "requirements.txt",
            "image_proxy.py", "selection_menus.py", "style_catalog.py", "style_enhancement.py",
            "PERSONA_MEMORY.md", "PROACTIVE_MODE.md", "BILIBILI_MODE.md", "WHALE_STICKERS.md",
            "PAPER_READER.md", "READING_CONTINUATION.md", "AUTO_READING.md", "MECHANISM_ASSISTANT.md", "BOT_COMMANDS.txt", "UPDATE_20261022.md", "smoke_bilibili.py", "smoke_paper_public.py", "smoke_model_decision_public.py", "apply_update.py",
        } or (path.name.startswith("test_") and path.suffix == ".py")
    if len(path.parts) == 2 and path.parts[0] in ("personas", "prompts"):
        return path.suffix == ".txt" and not path.name.startswith("_")
    return (len(path.parts) in (3, 4) and path.parts[:2] == ("resources", "whale_stickers")
            and (len(path.parts) == 3 or path.parts[2] == "black")
            and path.suffix.lower() in (".png", ".json", ".txt", ".md"))


def load_plan(bundle, target, include_personas=False):
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise ValueError("Empty or invalid manifest")
    if not (target / "bot.py").is_file() or not (target / "config.yaml").is_file():
        raise ValueError("Target must be the actual app directory containing bot.py and config.yaml")
    seen, plan = set(), []
    for entry in manifest["files"]:
        relative = entry["path"]
        if relative in seen or not permitted(relative):
            raise ValueError("Duplicate or non-allowlisted manifest path: " + relative)
        seen.add(relative)
        source, destination = inside(bundle / "payload", relative), inside(target, relative)
        if not source.is_file() or digest(source) != entry["sha256"]:
            raise ValueError("Bundle checksum mismatch: " + relative)
        if source.suffix == ".py":
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=relative)
        if source.suffix == ".json":
            json.loads(source.read_text(encoding="utf-8"))
        if source.suffix.lower() == ".png" and not source.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG: " + relative)
        if PurePosixPath(relative).parts[0] in ("personas", "prompts") and not include_personas:
            continue
        if destination.is_file() and digest(destination) == entry["sha256"]:
            continue
        plan.append((relative, source, destination))
    return plan


def apply_plan(target, plan):
    if not plan:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    backup = target / ("backup-vm-update-" + stamp)
    backup.mkdir()
    records = []
    # Back up every affected file before changing the first one.
    for relative, source, destination in plan:
        old = inside(backup / "files", relative)
        existed = destination.exists()
        if existed:
            if not destination.is_file():
                raise ValueError("Destination is not a regular file: " + relative)
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, old)
        records.append({"path": relative, "existed": existed, "old_sha256": digest(old) if existed else None,
                        "new_sha256": digest(source)})
    (backup / "rollback.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    applied = []
    try:
        for relative, source, destination in plan:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_name(destination.name + ".update-" + uuid.uuid4().hex[:8])
            try:
                shutil.copy2(source, temp)
                if digest(temp) != digest(source):
                    raise ValueError("Copy checksum mismatch: " + relative)
                temp.replace(destination)
            finally:
                temp.unlink(missing_ok=True)
            applied.append(relative)
    except Exception:
        for record in reversed(records):
            if record["path"] not in applied:
                continue
            dest = inside(target, record["path"])
            if record["existed"]:
                shutil.copy2(inside(backup / "files", record["path"]), dest)
            else:
                dest.unlink(missing_ok=True)
        raise
    return backup


def rollback(target, backup):
    backup = backup.resolve()
    if backup.parent != target.resolve() or not backup.name.startswith("backup-vm-update-"):
        raise ValueError("Backup must be an installer-created backup inside the target app directory")
    records = json.loads((backup / "rollback.json").read_text(encoding="utf-8"))
    # Check everything first; never silently replace changes made after this update.
    for row in records:
        if not permitted(row["path"]):
            raise ValueError("Unsafe rollback path")
        dest = inside(target, row["path"])
        if not dest.is_file() or digest(dest) != row["new_sha256"]:
            raise ValueError("File changed after update; manual review needed: " + row["path"])
        if row["existed"] and digest(inside(backup / "files", row["path"])) != row["old_sha256"]:
            raise ValueError("Backup checksum mismatch")
    for row in reversed(records):
        dest = inside(target, row["path"])
        if row["existed"]:
            shutil.copy2(inside(backup / "files", row["path"]), dest)
        else:
            dest.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply; otherwise show plan only")
    parser.add_argument("--include-personas", action="store_true", help="Also back up and replace attached persona/style TXT files")
    parser.add_argument("--rollback", type=Path, help="Restore one installer backup (stop only the Python bot first)")
    args = parser.parse_args()
    target = args.target.resolve()
    if args.rollback:
        if not args.apply:
            raise ValueError("Rollback requires --apply")
        rollback(target, args.rollback)
        print("Rollback completed. No services were restarted.")
        return
    bundle = Path(__file__).resolve().parent
    plan = load_plan(bundle, target, args.include_personas)
    for relative, _, destination in plan:
        print(("REPLACE " if destination.exists() else "ADD ") + relative)
    print(f"{len(plan)} file(s); config, credentials, history, runtime switches, and style selection are preserved.")
    if not args.apply:
        print("Dry run only. Inspect the plan, then add --apply to install.")
        return
    backup = apply_plan(target, plan)
    print("Backup: " + str(backup) if backup else "Already up to date.")
    print("No services were restarted. Install requirements in the bot's own Python environment, then restart only the Python bot.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Update stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
