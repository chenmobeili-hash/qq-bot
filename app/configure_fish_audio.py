"""Append a blank Fish Audio section without rewriting existing private configuration."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

import yaml

TEMPLATE = '''
# Fish Audio TTS; this section is private, never upload a real key to GitHub.
fish_audio:
  enabled: true
  api_key: ""
  api_key_env: FISH_AUDIO_API_KEY
  model: s2.1-pro-free
  voice_id: ""
  voices: {}
  default_voice: ""
  max_chars: 180
  max_seconds: 30
  timeout_seconds: 55
  daily_limit: 60
  scope_daily_limit: 20
  auto_cooldown_seconds: 60
  use_env_proxy: true
  proxy: ""
'''


def configure(root, apply=False):
    root = Path(root)
    path = root / 'config.yaml'
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Invalid configuration path; preserve and inspect manually')
    before = path.read_bytes()
    config = yaml.safe_load(before.decode('utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object')
    if 'fish_audio' in config:
        return {'status': 'preserved', '说明': 'fish_audio段已经存在，未改写已有开关、key或代理，请编辑原段'}
    if not apply:
        return {'status': 'preview', '说明': '仅准备在config.yaml末尾增加key留空的fish_audio段；原配置字节保留'}
    data = before + b'\n' + TEMPLATE.encode('utf-8')
    expected = dict(config)
    expected['fish_audio'] = yaml.safe_load(TEMPLATE)['fish_audio']
    if yaml.safe_load(data.decode('utf-8-sig')) != expected:
        raise ValueError('Appending would change existing fields; preserve and review manually')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backup = root / ('backup-vm-update-fish-config-' + stamp)
    backup.mkdir(mode=0o700)
    saved = backup / 'config.yaml'
    saved.write_bytes(before)
    os.chmod(saved, 0o600)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.fish_audio-config-', suffix='.tmp', dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != before:
            raise ValueError('Configuration changed during preparation; nothing replaced')
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {'status': 'added', '说明': '已增加key留空的fish_audio段；原配置已备份，其他字段未改', 'backup': backup.name,
            'original_sha256': hashlib.sha256(before).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        import json
        print(json.dumps(configure(args.target, args.apply), ensure_ascii=False))
    except Exception:
        print('{"status":"failed","reason":"configuration_not_changed; inspect the private file locally"}')
        raise SystemExit(1)
