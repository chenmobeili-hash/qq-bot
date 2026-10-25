"""Catch common runtime/private files before upload; not a content-secret detector."""
from pathlib import Path
import fnmatch
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
patterns = ('config.yaml', 'config_snowluma.yaml', 'conversations.json*', 'facts.txt',
            'autonomous_memory.json*', '*_state.json*', 'reading_sessions.json*',
            'bilibili_account.json*', 'bilibili_actions.json*', '_current_style.txt',
            'PROJECT_STATUS.md', '.env', '.env.*', '*.log', '*.bak*', '.aider*',
            '*.zip', '*.7z', '*.pem', '*.key', '*password*', 'screen*.png', 'screen*.jpg')
folders = ('backup', 'reading_sources', 'vision_image_cache', 'media_tmp', 'bilibili_tmp',
           'logs', 'downloads', 'uploads', 'updates', '.cursor', '.codex', '.agents')
def forbidden(path):
    if any(p.lower().startswith('backup') or p.lower() in folders for p in path.parts):
        return True
    return any(fnmatch.fnmatch(path.name.lower(), pattern.lower()) for pattern in patterns)

try:
    top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, check=False)
    own_repo = top.returncode == 0 and Path(top.stdout.decode('utf-8').strip()).resolve() == root
    tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=False) if own_repo else None
except OSError:
    tracked = None
if tracked is not None and tracked.returncode == 0:
    paths = [Path(p) for p in tracked.stdout.decode('utf-8').split('\0') if p]
    mode = 'tracked files'
else:
    paths = [p.relative_to(root) for p in root.rglob('*') if p.is_file() and not any(
        part in ('.git', '.venv', 'venv', '__pycache__', '.pytest_cache') for part in p.relative_to(root).parts)]
    mode = 'working directory files'
hits = sorted(str(p) for p in paths if forbidden(p))
print('Checked ' + mode + ': ' + str(len(paths)) + '; forbidden files: ' + str(len(hits)))
for path in hits:
    print(path)
sys.exit(bool(hits))
