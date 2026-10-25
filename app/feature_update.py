"""Preview/install the feature bundle while preserving VM paper-download customizations."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

import apply_update


def function_digest(source):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'extract_document')
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def merge_word_parser(current, template, baseline):
    """Replace only a recognized extractor and extension/help literals, never DNS/network code."""
    if function_digest(current) not in (baseline, function_digest(template)):
        raise ValueError('VM extract_document differs from both reviewed versions; merge Word parsing manually first')
    old_tree, new_tree = ast.parse(current), ast.parse(template)
    lines, new_lines = current.splitlines(keepends=True), template.splitlines(keepends=True)
    edits = []
    for node in old_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'extract_document':
            replacement = next(n for n in new_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'extract_document')
            edits.append((node.lineno - 1, node.end_lineno, ''.join(new_lines[replacement.lineno - 1:replacement.end_lineno])))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'EXTENSIONS' for t in node.targets):
            values = ast.literal_eval(node.value)
            if not isinstance(values, set) or not all(isinstance(v, str) for v in values):
                raise ValueError('Unexpected EXTENSIONS declaration')
            edits.append((node.lineno - 1, node.end_lineno, 'EXTENSIONS = {' + ', '.join(repr(v) for v in sorted(values | {'.docx', '.doc'})) + '}\n'))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'HELP' for t in node.targets):
            fragment = ''.join(lines[node.lineno - 1:node.end_lineno])
            if 'PDF/TXT/Markdown/DOCX' not in fragment:
                fragment = fragment.replace('PDF/TXT/Markdown', 'PDF/TXT/Markdown/DOCX（旧DOC需antiword）')
            edits.append((node.lineno - 1, node.end_lineno, fragment))
    if len(edits) != 3:
        raise ValueError('Missing or ambiguous Word merge anchors')
    for start, end, text in sorted(edits, reverse=True):
        lines[start:end] = [text]
    merged = ''.join(lines)
    ast.parse(merged)
    return merged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    target = args.target.resolve()
    if args.rollback:
        if not args.apply:
            raise ValueError('Rollback requires --apply')
        apply_update.rollback(target, args.rollback)
        print('Rollback complete. No services restarted.')
        return
    bundle = Path(__file__).resolve().parent
    apply_update.load_plan(bundle, target)  # Validate every payload hash/path before preparing.
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    baseline = manifest['word_extractor_baseline']
    current = (target / 'paper_reader.py').read_text(encoding='utf-8-sig')
    template = (bundle / 'payload' / 'paper_reader.py').read_text(encoding='utf-8-sig')
    merged = merge_word_parser(current, template, baseline)
    with tempfile.TemporaryDirectory(prefix='qq-feature-plan-', dir=bundle) as temporary:
        prepared = Path(temporary)
        (prepared / 'payload').mkdir()
        for entry in manifest['files']:
            src = apply_update.inside(bundle / 'payload', entry['path'])
            dst = apply_update.inside(prepared / 'payload', entry['path'])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if entry['path'] == 'paper_reader.py':
                dst.write_text(merged, encoding='utf-8')
                entry['sha256'] = apply_update.digest(dst)
            elif entry['path'] == '.gitignore':
                previous = (target / '.gitignore').read_text(encoding='utf-8-sig') if (target / '.gitignore').exists() else ''
                for pattern in ('hearing_sync_state.json', 'hearing_sync_state.tmp'):
                    if pattern not in previous.splitlines():
                        previous = previous.rstrip() + '\n' + pattern + '\n'
                dst.write_text(previous, encoding='utf-8')
                entry['sha256'] = apply_update.digest(dst)
        (prepared / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        plan = apply_update.load_plan(prepared, target)
        for name, _, destination in plan:
            print(('REPLACE ' if destination.exists() else 'ADD ') + name)
        print('Word extractor merged; existing paper-reader networking and DoH code preserved.')
        print(str(len(plan)) + ' file(s); configuration, personas and memory are excluded.')
        if args.apply:
            backup = apply_update.apply_plan(target, plan)
            print('Backup: ' + str(backup) if backup else 'Already current')
            print('No services restarted. Restart only Python bot when idle.')
        else:
            print('Preview only; add --apply to install.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('Update stopped: ' + str(exc))
