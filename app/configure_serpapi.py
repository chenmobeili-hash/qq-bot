"""Append a blank SerpApi section without rewriting existing private configuration."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

import yaml

TEMPLATE = '''
# Google Lens via SerpApi; fill only in your private configuration.
serpapi:
  enabled: true
  api_key: ""
  api_key_env: SERPAPI_API_KEY
  hl: zh-cn
  timeout_seconds: 55
  daily_limit: 20
  min_interval_seconds: 8
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
    if 'serpapi' in config:
        return {'status': 'preserved', '说明': 'serpapi段已经存在，未改写已有开关、key或代理，请编辑原段'}
    if not apply:
        return {'status': 'preview', '说明': '仅准备在config.yaml末尾增加key留空的serpapi段；原配置字节保留'}
    data = before + b'\n' + TEMPLATE.encode('utf-8')
    expected = dict(config)
    expected['serpapi'] = yaml.safe_load(TEMPLATE)['serpapi']
    if yaml.safe_load(data.decode('utf-8-sig')) != expected:
        raise ValueError('Appending would change existing fields; preserve and review manually')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backup = root / ('backup-vm-update-lens-config-' + stamp)
    backup.mkdir(mode=0o700)
    saved = backup / 'config.yaml'
    saved.write_bytes(before)
    os.chmod(saved, 0o600)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.serpapi-config-', suffix='.tmp', dir=root, delete=False) as stream:
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
    return {'status': 'added', '说明': '已增加key留空的serpapi段；原配置已备份，其他字段未改', 'backup': backup.name,
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
