import asyncio
import socket
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import bot
from image_network import ImageDoHResolver, ImageAddressError, ImageDoHUnavailable, public_answers, validate_image_url


def answer(*ips):
    return {'Status': 0, 'Answer': [{'type': 1, 'data': ip, 'TTL': 30} for ip in ips]}


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_public_rows_keep_original_hostname_and_port(self):
        events = []
        resolver = ImageDoHResolver(lambda event, **data: events.append((event, data)))
        with patch.object(resolver, '_query', AsyncMock(return_value=answer('8.8.8.8', '1.1.1.1'))):
            rows = await resolver.resolve('images.example', 443)
        self.assertEqual([r['host'] for r in rows], ['1.1.1.1'])
        self.assertTrue(all(r['hostname'] == 'images.example' and r['port'] == 443 and r['family'] == socket.AF_INET for r in rows))
        self.assertIn(('resolved', {'source': 'doh', 'addresses': 2}), events)
        self.assertIn(('dial_selected', {'source': 'doh', 'dialed': '1.1.1.1', 'rank': 1}), events)

    async def test_numeric_rank_is_stable_across_dns_order_and_duplicates(self):
        permutations = [('8.8.8.8', '1.1.1.20', '1.1.1.3'), ('1.1.1.20', '1.1.1.3', '8.8.8.8'),
                        ('1.1.1.3', '8.8.8.8', '1.1.1.20', '1.1.1.3')]
        for ips in permutations:
            for attempt, expected in ((1, '1.1.1.3'), (2, '1.1.1.20'), (3, '8.8.8.8')):
                with self.subTest(ips=ips, attempt=attempt):
                    resolver = ImageDoHResolver(attempt=attempt)
                    with patch.object(resolver, '_query', AsyncMock(return_value=answer(*ips))):
                        rows = await resolver.resolve('images.example', 443)
                    self.assertEqual([row['host'] for row in rows], [expected])

    async def test_single_member_remains_usable_on_second_attempt(self):
        resolver = ImageDoHResolver(attempt=2)
        with patch.object(resolver, '_query', AsyncMock(return_value=answer('8.8.8.8'))):
            rows = await resolver.resolve('images.example', 443)
        self.assertEqual([row['host'] for row in rows], ['8.8.8.8'])

    async def test_invalid_unselected_member_is_still_rejected_before_ranking(self):
        for attempt in (1, 2):
            resolver = ImageDoHResolver(attempt=attempt)
            with patch.object(resolver, '_query', AsyncMock(return_value=answer('8.8.8.8', '1.1.1.1', '10.0.0.1'))):
                with self.assertRaises(ImageAddressError):
                    await resolver.resolve('images.example', 443)

    def test_invalid_attempt_is_rejected(self):
        for attempt in (0, -1, True, 1.5, '2'):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                ImageDoHResolver(attempt=attempt)

    async def test_mixed_private_mapping_reserved_and_empty_answers_fail_closed(self):
        bad = [answer('8.8.8.8', '10.0.0.1'), answer('198.18.59.148'), answer('127.0.0.1'), answer(),
               {'Status': 3}, {'Status': 0, 'Answer': [{'type': 1, 'data': 'invalid'}]},
               {'Status': 0, 'Answer': [{'type': 28, 'data': '::1'}, {'type': 1, 'data': '8.8.8.8'}]}]
        for payload in bad:
            with self.subTest(payload=payload):
                resolver = ImageDoHResolver()
                with patch.object(resolver, '_query', AsyncMock(return_value=payload)) as query:
                    with self.assertRaises(ImageAddressError):
                        await resolver.resolve('images.example', 443)
                self.assertEqual(query.await_count, 1)  # Never ask another provider to bypass rejection.

    async def test_no_system_fallback_after_both_doh_endpoints_fail(self):
        resolver = ImageDoHResolver()
        with patch.object(resolver, '_query', AsyncMock(side_effect=aiohttp.ClientConnectionError('SECRET'))) as query, \
             patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock()) as system:
            with self.assertRaises(ImageDoHUnavailable):
                await resolver.resolve('images.example', 443)
        self.assertEqual(query.await_count, 2)
        system.assert_not_awaited()

    async def test_second_provider_can_resolve_after_transport_failure(self):
        resolver = ImageDoHResolver()
        with patch.object(resolver, '_query', AsyncMock(side_effect=[asyncio.TimeoutError(), answer('1.1.1.1')])):
            rows = await resolver.resolve('images.example', 443)
        self.assertEqual(rows[0]['host'], '1.1.1.1')

    async def test_retry_gets_fresh_addresses_instead_of_static_or_cached_ips(self):
        resolver = ImageDoHResolver()
        with patch.object(resolver, '_query', AsyncMock(side_effect=[answer('8.8.8.8'), answer('1.1.1.1')])) as query:
            first = await resolver.resolve('images.example', 443)
            second = await resolver.resolve('images.example', 443)
        self.assertNotEqual(first[0]['host'], second[0]['host'])
        self.assertEqual(query.await_count, 2)

    async def test_tls_sni_host_and_ca_verification_survive_ip_pinning(self):
        # Intercept immediately before the socket is opened; no connection to these example IPs.
        resolver = ImageDoHResolver(attempt=2)
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        signed = 'https://images.example/download?sig=ABC%2Fdef&x=1'
        with patch.object(resolver, '_query', AsyncMock(return_value=answer('9.9.9.9', '8.8.8.8', '1.1.1.1'))), \
             patch.object(connector, '_wrap_create_connection', AsyncMock(side_effect=RuntimeError('fixture stop'))) as connect:
            async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
                with self.assertRaisesRegex(RuntimeError, 'fixture stop'):
                    await session.get(signed)
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs['server_hostname'], 'images.example')
        self.assertEqual(kwargs['req'].headers['Host'], 'images.example')
        self.assertEqual(kwargs['addr_infos'][0][4][0], '8.8.8.8')
        self.assertEqual(len(kwargs['addr_infos']), 1)  # No hidden connector fallback to other pool members.
        self.assertIsInstance(kwargs['ssl'], ssl.SSLContext)
        self.assertTrue(kwargs['ssl'].check_hostname)
        self.assertEqual(kwargs['ssl'].verify_mode, ssl.CERT_REQUIRED)

    async def test_doh_only_uses_environment_proxy(self):
        real_session = aiohttp.ClientSession
        captures = []
        def session(**kwargs):
            captures.append(kwargs)
            return real_session(**kwargs)
        resolver = ImageDoHResolver()
        with patch('image_network.aiohttp.ClientSession', side_effect=session), patch.object(resolver, '_query', AsyncMock(return_value=answer('1.1.1.1'))):
            await resolver.resolve('images.example', 443)
        self.assertTrue(captures[0]['trust_env'])
        self.assertEqual(captures[0]['timeout'].total, 4)

    async def test_cancel_does_not_fall_back_to_another_provider(self):
        resolver = ImageDoHResolver()
        with patch.object(resolver, '_query', AsyncMock(side_effect=asyncio.CancelledError)) as query:
            with self.assertRaises(asyncio.CancelledError):
                await resolver.resolve('images.example', 443)
        self.assertEqual(query.await_count, 1)


class URLTests(unittest.TestCase):
    def test_signed_url_is_unchanged(self):
        url = 'https://images.example/download?sig=A%2fb%2B&x=001'
        self.assertEqual(validate_image_url(url), url)

    def test_unsafe_targets_are_rejected_even_without_dns(self):
        for url in ('http://127.0.0.1/x', 'http://198.18.59.147/x', 'http://[::1]/', 'file:///tmp/x',
                    'https://user:password@images.example/x', 'https://localhost/x', 'https://x.local/x'):
            with self.subTest(url=url), self.assertRaises(ImageAddressError):
                validate_image_url(url)

    def test_cname_with_public_a_is_accepted(self):
        payload = answer('8.8.8.8')
        payload['Answer'].insert(0, {'type': 5, 'data': 'cdn.example'})
        self.assertEqual(public_answers(payload), ['8.8.8.8'])


if __name__ == '__main__':
    unittest.main()
