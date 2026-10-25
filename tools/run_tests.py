"""Collect unittest classes only; never import manual scripts that send QQ messages."""
import argparse
import ast
import json
from pathlib import Path
import sys
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1] / 'app')
parser.add_argument('--output', type=Path, default=Path('test-results.json'))
args = parser.parse_args()
directory = args.directory.resolve()
sys.path.insert(0, str(directory))
modules, manual = [], []
for path in sorted(directory.glob('test_*.py')):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    if any(isinstance(node, ast.ClassDef) and any(ast.unparse(base).endswith('TestCase') for base in node.bases)
           for node in tree.body):
        modules.append(path.stem)
    else:
        manual.append(path.name)
suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
with args.output.with_suffix('.log').open('w', encoding='utf-8') as log:
    result = unittest.TextTestRunner(stream=log, verbosity=1).run(suite)
summary = {'run': result.testsRun, 'modules': len(modules),
           'failures': [test.id() for test, _ in result.failures],
           'errors': [test.id() for test, _ in result.errors],
           'skipped': [test.id() for test, _ in result.skipped], 'manual_excluded': manual}
args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
sys.exit(0 if result.wasSuccessful() else 1)
