import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, Mock

from proactive import ProactiveMode
from paper_autonomy import PaperAutonomy
from paper_reader import PaperLibrary


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.clock=1800000000.0
        self.now=datetime(2026,10,26,12,tzinfo=timezone(timedelta(hours=8)))
        self.cfg={'deepseek':{'enabled':True},'proactive':{'groups':[123],
            'wake_min_seconds':7200,'wake_max_seconds':7200},
            'paper_reader':{'auto_read':{'probability':0.0000000001}}}
        self.mode=ProactiveMode(self.root/'chat.json',Mock(),clock=lambda:self.clock)

    async def test_repeated_seeding_never_changes_overdue_deadline(self):
        self.mode.seed(self.cfg)
        original=self.mode.wake_due['123']
        self.clock=original+600
        for _ in range(3):
            self.mode=ProactiveMode(self.mode.path,Mock(),clock=lambda:self.clock)
            self.mode.seed(self.cfg)
            self.assertEqual(self.mode.wake_due['123'],original)

    async def test_overdue_wake_runs_once_then_keeps_original_time_grid(self):
        self.mode.seed(self.cfg)
        original=self.mode.wake_due['123']
        self.clock=original+600
        self.mode=ProactiveMode(self.mode.path,Mock(),clock=lambda:self.clock)
        generate=AsyncMock(return_value='这次补一次')
        send=AsyncMock(return_value=True)
        await self.mode.tick(lambda:self.cfg,lambda c:self.now,lambda:True,generate,send)
        self.assertEqual(self.mode.wake_due['123'],original+7200)
        await self.mode.tick(lambda:self.cfg,lambda c:self.now,lambda:True,generate,send)
        send.assert_awaited_once()

    async def test_silent_hours_keep_the_original_overdue_slot(self):
        self.mode.seed(self.cfg)
        original=self.mode.wake_due['123']
        self.clock=original+600
        generate=AsyncMock(return_value='台词')
        send=AsyncMock(return_value=True)
        await self.mode.tick(lambda:self.cfg,lambda c:self.now.replace(hour=23),lambda:True,generate,send)
        self.assertEqual(self.mode.wake_due['123'],original)
        self.assertNotIn('123',self.mode.wake_daily)
        generate.assert_not_awaited()

    async def test_reading_recovers_missed_checks_and_guarantee_in_one_poll(self):
        self.mode.seed(self.cfg)
        reading=PaperAutonomy(self.root/'read.json',Mock(),clock=lambda:self.clock)
        reading.ensure_schedule(123,self.cfg,self.mode)
        original=reading.next_check(123,self.cfg)
        # Both preliminary checkpoints and the guaranteed checkpoint occurred offline.
        self.clock=original+2*7200+600
        reading=PaperAutonomy(reading.path,Mock(),clock=lambda:self.clock)
        self.mode=ProactiveMode(self.mode.path,Mock(),clock=lambda:self.clock)
        discover=AsyncMock(return_value=[])
        await reading.tick(lambda:self.cfg,lambda c:self.now,lambda:True,self.mode,PaperLibrary(),
            discover,AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(),lambda c:1)
        discover.assert_awaited_once()
        self.assertEqual(reading.groups['123']['checks'],3)
        self.assertEqual(reading.groups['123']['next_check_at'],original+3*7200)

    async def test_recovery_waits_for_conditions_without_consuming_guarantee(self):
        self.mode.seed(self.cfg)
        reading=PaperAutonomy(self.root/'read.json',Mock(),clock=lambda:self.clock)
        reading.ensure_schedule(123,self.cfg,self.mode)
        original=reading.next_check(123,self.cfg)
        self.clock=original+2*7200+600
        discover=AsyncMock(return_value=[])
        await reading.tick(lambda:self.cfg,lambda c:self.now.replace(hour=23),lambda:True,self.mode,
            PaperLibrary(),discover,AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(),lambda c:1)
        self.assertEqual(reading.next_check(123,self.cfg),original)
        self.assertNotIn('checks',reading.groups['123'])
        discover.assert_not_awaited()


if __name__=='__main__':unittest.main()
