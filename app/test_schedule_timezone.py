import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import types

import bot
import schedule


BEIJING = timezone(timedelta(hours=8))


class ScheduleTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = schedule.Scheduler()
        self.patcher = patch.object(schedule, 'default_scheduler', self.scheduler)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_evening_task_runs_at_beijing_time_on_new_york_host(self):
        before = datetime(2026, 10, 9, 17, 52, tzinfo=BEIJING)
        callback = Mock()
        with patch.object(bot, '_bot_now', return_value=before):
            job = bot.ConfiguredDailyJob({}).day.at('17:59').do(callback)
        expected = datetime(2026, 10, 9, 17, 59, tzinfo=BEIJING)
        self.assertEqual(job.next_run.astimezone(BEIJING), expected)

        class HostClock(datetime):
            @classmethod
            def now(cls, tz=None):
                current = expected - timedelta(seconds=1)
                return current.astimezone(tz) if tz else current.astimezone().replace(tzinfo=None)
        fake_module = types.SimpleNamespace(datetime=HostClock)
        with patch.object(schedule, 'datetime', fake_module):
            self.scheduler.run_pending()
        callback.assert_not_called()

        class DueClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return expected.astimezone(tz) if tz else expected.astimezone().replace(tzinfo=None)
        with patch.object(schedule, 'datetime', types.SimpleNamespace(datetime=DueClock)), \
             patch.object(bot, '_bot_now', return_value=expected):
            self.scheduler.run_pending()
            self.scheduler.run_pending()
        callback.assert_called_once()
        self.assertEqual(job.next_run.astimezone(BEIJING), expected + timedelta(days=1))

    def test_registration_after_due_time_does_not_send_old_greeting(self):
        now = datetime(2026, 10, 9, 18, 0, tzinfo=BEIJING)
        callback = Mock()
        with patch.object(bot, '_bot_now', return_value=now):
            job = bot.ConfiguredDailyJob({}).day.at('17:59').do(callback)
        self.assertEqual(job.next_run.astimezone(BEIJING), datetime(2026, 10, 10, 17, 59, tzinfo=BEIJING))
        callback.assert_not_called()

    def test_configured_other_offset_and_year_rollover(self):
        utc = timezone.utc
        now = datetime(2026, 12, 31, 23, 59, 50, tzinfo=utc)
        with patch.object(bot, '_bot_now', return_value=now):
            job = bot.ConfiguredDailyJob({'timezone_offset_hours': 0}).day.at('00:00').do(Mock())
        self.assertEqual(job.next_run.astimezone(utc), datetime(2027, 1, 1, tzinfo=utc))

    def test_all_modes_register_with_configured_clock(self):
        config = {'schedule': [{'time': '08:59', 'mode': 'ai'},
                               {'time': '11:59', 'mode': 'fixed', 'content': '午安'},
                               {'time': '17:59', 'mode': 'random'}],
                  'target_group_ids': [123]}
        now = datetime(2026, 10, 9, 6, 0, tzinfo=BEIJING)
        with patch.object(bot, '_bot_now', return_value=now), patch.object(bot, 'log'), \
             patch.object(bot, 'load_messages', return_value=['测试素材']):
            manager = bot.Scheduler(config)
            manager.setup()
        self.assertEqual(len(self.scheduler.jobs), 3)
        self.assertTrue(all(isinstance(job, bot.ConfiguredDailyJob) for job in self.scheduler.jobs))
        self.assertEqual([job.next_run.astimezone(BEIJING).strftime('%H:%M')
                          for job in self.scheduler.jobs], ['08:59', '11:59', '17:59'])


if __name__ == '__main__':
    unittest.main()
