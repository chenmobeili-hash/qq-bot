import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import bot


class DeliveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_history = bot.recent_sent_history
        bot.recent_sent_history = []
        self.log_patch = patch.object(bot, 'log')
        self.log_patch.start()

    async def asyncTearDown(self):
        bot.recent_sent_history = self.old_history
        self.log_patch.stop()

    async def test_disconnected_websocket_does_not_block_http_fallback(self):
        with patch.object(bot, 'ws_connection', None):
            self.assertFalse(await bot.send_group_message(123, '晚安'))
        self.assertFalse(bot.is_duplicate_send(123, '晚安'))

    async def test_explicit_failure_does_not_block_http_fallback(self):
        async def rejected(action, params, *, outcome):
            outcome['status'] = 'failed'
            return None
        with patch.object(bot, 'call_api', side_effect=rejected):
            self.assertFalse(await bot.send_group_message(123, '晚安'))
        self.assertFalse(bot.is_duplicate_send(123, '晚安'))

    async def test_uncertain_delivery_still_prevents_duplicate(self):
        async def uncertain(action, params, *, outcome):
            outcome['status'] = 'unknown'
            return None
        with patch.object(bot, 'call_api', side_effect=uncertain):
            self.assertFalse(await bot.send_group_message(123, '晚安'))
        self.assertTrue(bot.is_duplicate_send(123, '晚安'))

    async def test_confirmed_success_is_deduplicated(self):
        api = AsyncMock(return_value={'message_id': 1})
        with patch.object(bot, 'call_api', api):
            self.assertTrue(await bot.send_group_message(123, '晚安'))
            self.assertTrue(await bot.send_group_message(123, '晚安'))
        self.assertEqual(api.await_count, 1)

    def test_http_uses_separate_token_and_correct_default_port(self):
        config = {'napcat': {'access_token': 'ws-token', 'api_http_token': 'http-token'}}
        parsed = urlsplit(bot._build_http_url(config, '/get_login_info'))
        self.assertEqual(parsed.port, 3000)
        self.assertEqual(parse_qs(parsed.query)['access_token'], ['http-token'])
        config['napcat']['api_http_token'] = ''
        self.assertEqual(urlsplit(bot._build_http_url(config, '/get_login_info')).query, '')


if __name__ == '__main__':
    unittest.main()
