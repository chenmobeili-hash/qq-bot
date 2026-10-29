"""Offline regression tests: no QQ sends and no real model calls."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bot
import long_term_memory as memory
from model_decision import DecisionError, parse
import test_long_term_memory as existing


class DecayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = 'private_7'
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def row(self, text='喜欢时间哲学', age=0, **meta):
        return dict(text=text, updated_at=(self.now - timedelta(days=age)).isoformat(), source=self.key, **meta)

    def save(self, bucket, identity=None, version=1):
        (self.root / memory.FILE).write_text(json.dumps(
            {'version': version, 'identity': identity or {}, 'personal': {self.key: bucket}}), encoding='utf-8')

    def write(self, ops, admin=False, query=''):
        return memory.commit(self.root, self.key, ops, {}, admin, memory.snapshot(self.root, self.key, query))

    def at(self):
        mocked = patch.object(memory, 'datetime', wraps=datetime)
        clock = self.enterContext(mocked)
        clock.now.return_value = self.now
        return clock

    def test_half_life_and_importance_are_monotonic(self):
        now = self.now.timestamp()
        self.assertAlmostEqual(memory.retention(self.row(age=21), now), .5)
        self.assertGreater(memory.retention(self.row(age=60, importance=5), now), memory.retention(self.row(age=60), now))
        self.assertLess(memory.retention(self.row(age=60), now), memory.retention(self.row(age=20), now))
        self.assertGreater(memory.retention(self.row(age=60, uses=5), now), memory.retention(self.row(age=60), now))

    def test_stable_notes_and_identity_survive_years(self):
        self.at()
        self.save({'约定': self.row('长期约定', age=5000, durable=True)},
                  {'名字': self.row('我是汐蓝', age=5000)})
        self.assertIn('长期约定', memory.personal_prompt(self.root, self.key, {}))
        self.assertIn('我是汐蓝', memory.identity_prompt(self.root, {}))

    def test_old_unrelated_note_dormant_but_related_topic_recalls_it(self):
        self.at()
        self.save({'兴趣': self.row('喜欢海洋生物研究', age=1000)})
        self.assertNotIn('喜欢海洋生物研究', memory.personal_prompt(self.root, self.key, {}, '晚饭吃什么'))
        self.assertIn('喜欢海洋生物研究', memory.personal_prompt(self.root, self.key, {}, '海洋生物有什么趣事'))
        self.assertEqual(len(memory._read(self.root)['personal'][self.key]), 1)

    def test_relevance_outweighs_unrelated_recent_notes_and_budgets_hold(self):
        bucket = {str(i): self.row('普通新事' + str(i), durable=True) for i in range(23)}
        bucket['音乐'] = self.row('喜欢 Debussy 钢琴音乐', age=2000)
        selected = memory.select_notes(bucket, 'Debussy 的音乐', self.now.timestamp())
        self.assertEqual(selected[0][0], '音乐')
        self.assertLessEqual(len(selected), 6)
        bucket = {str(i): self.row(str(i) + '文' * 499) for i in range(12)}
        self.assertLessEqual(sum(len(r['text']) for _, r in memory.select_notes(bucket)), 1800)

    def test_reading_prompt_never_rewrites_or_strengthens(self):
        self.save({'兴趣': self.row(age=5)})
        before = (self.root / memory.FILE).read_bytes()
        for _ in range(3):
            memory.personal_prompt(self.root, self.key, {}, '时间哲学')
            memory.snapshot(self.root, self.key, '时间哲学')
            memory.diagnostic(self.root, self.key, {})
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())

    def test_old_format_is_loaded_without_migration_write(self):
        self.save({'兴趣': self.row()})
        before = (self.root / memory.FILE).read_bytes()
        self.assertIn('喜欢时间哲学', memory.personal_prompt(self.root, self.key, {}))
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.write([dict(existing.note(), importance=5, durable=True)])
        state = memory._read(self.root)
        self.assertEqual(state['version'], 2)
        self.assertEqual(state['personal'][self.key]['兴趣']['updated_at'], self.row()['updated_at'])

    def test_actual_usage_persists_and_repeated_usage_within_day_is_noop(self):
        self.at()
        self.save({'兴趣': self.row(age=40)})
        op = {'op': 'recall', 'scope': 'personal', 'key': '兴趣'}
        self.assertEqual(self.write([op]), 1)
        before = (self.root / memory.FILE).read_bytes()
        self.assertEqual(self.write([op]), 0)
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.assertEqual(memory._read(self.root)['personal'][self.key]['兴趣']['uses'], 1)
        self.assertAlmostEqual(memory.retention(memory._read(self.root)['personal'][self.key]['兴趣'], self.now.timestamp()), 1)

    def test_reinforcement_uses_saved_wall_clock_across_restart(self):
        clock = self.at()
        self.save({'兴趣': self.row(age=40)})
        op = {'op': 'recall', 'scope': 'personal', 'key': '兴趣'}
        self.write([op])
        clock.now.return_value = self.now + timedelta(hours=23)
        self.assertEqual(self.write([op]), 0)
        clock.now.return_value = self.now + timedelta(hours=25)
        self.assertEqual(self.write([op]), 1)
        self.assertEqual(memory._read(self.root)['personal'][self.key]['兴趣']['uses'], 2)

    def test_rewrite_identical_note_cannot_reset_age(self):
        self.at()
        self.save({'兴趣': self.row(age=1000)})
        op = existing.note(key='兴趣', text='喜欢时间哲学')
        self.write([op])
        row = memory._read(self.root)['personal'][self.key]['兴趣']
        self.assertEqual(row['updated_at'], self.row(age=1000)['updated_at'])
        before = (self.root / memory.FILE).read_bytes()
        self.assertEqual(self.write([op]), 0)
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())

    def test_correcting_same_label_replaces_instead_of_duplicating(self):
        self.at()
        self.save({'兴趣': self.row(age=50, uses=8, last_used_at=self.row(age=40)['updated_at'])})
        self.write([existing.note(key='兴趣', text='现在偏好语言哲学')])
        bucket = memory._read(self.root)['personal'][self.key]
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket['兴趣']['uses'], 0)
        self.assertEqual(bucket['兴趣']['updated_at'], self.now.isoformat(timespec='seconds'))

    def test_unoffered_or_nonexistent_note_cannot_be_reinforced(self):
        self.at()
        self.save({'兴趣': self.row(age=1000)})
        before = (self.root / memory.FILE).read_bytes()
        for name in ('兴趣', '不存在'):
            with self.assertRaises(memory.MemoryUnavailable):
                self.write([{'op': 'recall', 'scope': 'personal', 'key': name}])
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.assertEqual(self.write([{'op': 'recall', 'scope': 'personal', 'key': '兴趣'}], query='时间哲学'), 1)

    def test_nonadmin_may_use_but_cannot_change_identity(self):
        self.save({}, {'名字': self.row('我是汐蓝')})
        self.write([{'op': 'recall', 'scope': 'identity', 'key': '名字'}])
        self.assertEqual(memory._read(self.root)['identity']['名字']['text'], '我是汐蓝')
        for op in ('remember', 'forget'):
            value = existing.note('identity', '名字') if op == 'remember' else {'op': op, 'scope': 'identity', 'key': '名字'}
            with self.assertRaises(ValueError):
                self.write([value])

    def test_failed_mixed_recall_rolls_back_other_changes(self):
        self.save({'兴趣': self.row()})
        before = (self.root / memory.FILE).read_bytes()
        with self.assertRaises(memory.MemoryUnavailable):
            self.write([existing.note(key='新事'), {'op': 'recall', 'scope': 'personal', 'key': '不存在'}])
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())

    def test_protocol_accepts_metadata_and_recall_but_not_arbitrary_fields(self):
        op = dict(existing.note(), importance=4, durable=True)
        self.assertEqual(parse(existing.raw([op]), self.key, {}, False).memory, [op])
        recall = {'op': 'recall', 'scope': 'personal', 'key': '爱好'}
        self.assertEqual(parse(existing.raw([recall]), self.key, {}, False).memory, [recall])
        for meta in ({'importance': 0}, {'importance': True}, {'durable': 'yes'}, {'uses': 100}, {'importance': 6}):
            result = parse(existing.raw([dict(existing.note(), **meta)]), self.key, {}, False)
            self.assertEqual(result.memory, [])
            self.assertIsNotNone(result.memory_error)

    def test_corrupt_metadata_is_preserved_and_disabled_hides_all(self):
        self.save({'兴趣': self.row(importance=100)})
        before = (self.root / memory.FILE).read_bytes()
        with self.assertRaises(memory.MemoryUnavailable):
            memory.snapshot(self.root, self.key)
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.assertEqual(memory.personal_prompt(self.root, self.key, {'long_term_memory': {'enabled': False}}), '')

    def test_clock_rollback_never_yields_more_than_full_strength(self):
        self.assertEqual(memory.retention(self.row(age=-10), self.now.timestamp()), 1)
        self.assertLess(memory.retention({'text': '旧事', 'updated_at': 'invalid'}, self.now.timestamp()), .12)

    def test_diagnostic_is_readonly_and_does_not_expose_private_text(self):
        self.at()
        self.save({'兴趣': self.row('机密笔记正文', age=1000)})
        value = memory.diagnostic(self.root, self.key, {})
        self.assertIn('休眠1条', value)
        self.assertNotIn('机密笔记', value)

    def test_explicit_legacy_export_keeps_every_note_and_does_not_replace_live_file(self):
        self.save({'兴趣': self.row(age=1000, uses=3, durable=False)},
                  {'名字': self.row('我是汐蓝', durable=True)}, version=2)
        before = (self.root / memory.FILE).read_bytes()
        output = memory.export_legacy(self.root)
        legacy = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(legacy['version'], 1)
        self.assertEqual(set(legacy['personal'][self.key]['兴趣']), {'text', 'updated_at', 'source'})
        self.assertEqual(legacy['identity']['名字']['text'], '我是汐蓝')
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.assertTrue(output.parent.name.startswith('backup-vm-update-'))

    def test_recall_metadata_does_not_change_persona_signature(self):
        self.save({}, {'名字': self.row('我是汐蓝')})
        before = memory.identity_prompt(self.root, {})
        self.write([{'op': 'recall', 'scope': 'identity', 'key': '名字'}])
        self.assertEqual(before, memory.identity_prompt(self.root, {}))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.IntegrationTests.setUp

    async def test_main_model_receives_query_selected_note_and_can_reinforce(self):
        now = datetime.now(timezone.utc)
        path = self.root / memory.FILE
        row = {'text': '喜欢海洋生物研究', 'updated_at': (now - timedelta(days=500)).isoformat(), 'source': 'private_7'}
        path.write_text(json.dumps({'version': 1, 'identity': {}, 'personal': {'private_7': {'兴趣': row}}}), encoding='utf-8')
        self.create.return_value = existing.response(existing.raw([{'op': 'recall', 'scope': 'personal', 'key': '兴趣'}], reply='记得你对海洋生物很感兴趣。'))
        await bot.ask_deepseek('private_7', '海洋生物有意思吗')
        self.assertIn('喜欢海洋生物研究', self.create.call_args.kwargs['messages'][0]['content'])
        self.assertEqual(memory._read(self.root)['personal']['private_7']['兴趣']['uses'], 1)

    async def test_precise_reading_context_cannot_be_copied_to_permanent_memory(self):
        with patch.object(bot.reading_bridge, 'history_expiry', return_value=datetime.now(timezone.utc).timestamp() + 600):
            self.create.return_value = existing.response(existing.raw([existing.note(text='某页临时详细原文')], reply='这一页讨论了时间。'))
            result = await bot.ask_deepseek('private_7', '解释这页')
        self.assertIn('未另存永久笔记', result)
        self.assertFalse((self.root / memory.FILE).exists())
        self.assertTrue(any('memory不能包含remember' in r['content'] for r in self.create.call_args.kwargs['messages']))


if __name__ == '__main__':
    unittest.main()
