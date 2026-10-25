"""Install download changes while retaining the target VM's resolver/DoH implementation."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import apply_update as update

DELEGATE = '''async def fetch_bytes(url):
    from paper_download import fetch_document
    return await fetch_document(url, resolver_factory=PublicResolver,
        validate_url=public_url, error=PaperError, max_bytes=MAX_BYTES)
'''


def fingerprint(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def patch_source(text, accepted):
    tree = ast.parse(text)
    functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'fetch_bytes']
    if len(functions) != 1:
        raise ValueError('Cannot uniquely identify fetch_bytes; manual merge required')
    original = functions[0]
    if original.decorator_list:
        raise ValueError('Custom fetch decorator found; manual merge required')
    replacement = ast.parse(DELEGATE).body[0]
    current = fingerprint(original)
    if current == fingerprint(replacement):
        return text
    if current not in accepted:
        raise ValueError('VM fetch_bytes has custom logic. Nothing was installed; manually merge the bounded downloader, preserving VM-specific settings. PublicResolver/DoH must be kept.')
    rows = text.splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    rows[original.lineno - 1:original.end_lineno] = [DELEGATE.replace('\n', newline)]
    result = ''.join(rows)
    ast.parse(result)
    # No edits to any other top-level definition, including the VM's resolver and certificate helpers.
    def others(source):
        return [ast.dump(n, include_attributes=False) for n in ast.parse(source).body
                if not (isinstance(n, ast.AsyncFunctionDef) and n.name == 'fetch_bytes')]
    if others(text) != others(result):
        raise ValueError('Patch changed non-download code')
    return result


def install(bundle, target, apply=False):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required; nothing installed')
    bundle, target = Path(bundle), Path(target).resolve()
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    # Validate every packaged file and target directory before preparing the generated local patch.
    plan = update.load_plan(bundle, target)
    for row in manifest['files']:
        dest = update.inside(target, row['path'])
        if dest.is_file() and update.digest(dest) not in (row.get('base_sha256'), row['sha256']):
            raise ValueError('Local modifications detected; merge first, nothing installed: ' + row['path'])
    source = update.inside(target, 'paper_reader.py')
    if not source.is_file():
        raise ValueError('Target paper_reader.py missing')
    original = source.read_bytes().decode('utf-8-sig')
    patched = patch_source(original, manifest['download_patch']['accepted_fetch_fingerprints'])
    with tempfile.TemporaryDirectory(prefix='qq-download-patch-') as temp:
        generated = Path(temp) / 'paper_reader.py'
        if patched != original:
            generated.write_bytes(patched.encode('utf-8'))
            plan.append(('paper_reader.py', generated, source))
        for relative, _, dest in plan:
            print(('REPLACE ' if dest.exists() else 'ADD ') + relative)
        if not apply:
            print('Preview only; original resolver and all non-download definitions are preserved.')
            return None
        backup = update.apply_plan(target, plan)
    print('Backup:', backup)
    print('DoH resolver, config, personas and runtime state preserved. No service restarted.')
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if args.rollback:
        if not args.apply:
            raise ValueError('Rollback requires --apply')
        update.rollback(args.target.resolve(), args.rollback)
        print('Rollback complete; no service restarted.')
        return
    install(Path(__file__).resolve().parent, args.target, args.apply)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Update stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
