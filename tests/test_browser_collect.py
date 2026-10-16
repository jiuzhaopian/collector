import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_collect as b
import collector as c
from test_continue_collect import FixtureClient, URL, page


class StubPage:
    def set_default_navigation_timeout(self, timeout):
        pass

    def route(self, pattern, callback):
        self.callback = callback


class StubContext:
    def new_page(self):
        return StubPage()


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = c.Store(self.tmp.name)
        self.view = b.BrowserStore(self.store)
        self.key = c.digest(URL.encode())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_old_tls_error_gets_one_browser_attempt_without_deleting_evidence(self):
        old = dict(status='error', error='[SSL: UNEXPECTED_EOF_WHILE_READING]', url=URL)
        self.store.put('continuation_detail', self.key, old)
        self.assertIsNone(self.view.get('continuation_detail', self.key))
        self.assertEqual(self.store.get('continuation_detail', self.key), old)
        self.store.put('browser_detail_attempt', self.key, dict(url=URL))
        self.assertEqual(self.view.get('continuation_detail', self.key), old)

    def test_other_errors_are_not_silently_retried(self):
        old = dict(status='error', error='HTTP 404')
        self.store.put('continuation_detail', self.key, old)
        self.assertEqual(self.view.get('continuation_detail', self.key), old)

    def test_browser_provenance_is_separate_from_script_http(self):
        self.view.put('announcement', 'fixture', dict(evidence='live_http'))
        self.assertEqual(self.store.get('announcement', 'fixture')['evidence'], 'live_browser_response')
        self.view.put('attachment', 'fixture', dict(evidence='synthetic_test'))
        self.assertEqual(self.store.get('attachment', 'fixture')['evidence'], 'synthetic_test')

    def test_saved_host_block_stops_before_navigation(self):
        self.store.put('host', 'www.celma.org.cn', dict(blocked=True, reason='418'))
        client = b.BrowserClient(self.view, StubContext())
        with patch.object(client, '_read_browser') as read:
            with self.assertRaises(c.AccessStopped):
                client.fetch(URL)
            read.assert_not_called()

    def test_robots_denial_stops_before_detail_read(self):
        client = b.BrowserClient(self.view, StubContext())
        with patch.object(client, '_read_browser', return_value=(200, b'User-agent: *\nDisallow: /fxqgg/')) as read:
            with self.assertRaises(c.AccessStopped):
                client.fetch(URL)
            self.assertEqual(read.call_count, 1)
        self.assertTrue(self.store.get('host', 'www.celma.org.cn')['blocked'])

    def test_unavailable_robots_is_not_a_skippable_page_error(self):
        client = b.BrowserClient(self.view, StubContext())
        with patch.object(client, '_read_browser', side_effect=OSError('connection closed')):
            with self.assertRaises(c.AccessStopped):
                client.fetch(URL)
        self.assertTrue(self.store.get('host', 'www.celma.org.cn')['blocked'])

    def test_robots_crawl_delay_and_404_policy(self):
        client = b.BrowserClient(self.view, StubContext())
        with patch.object(client, '_read_browser', side_effect=[(200, b'User-agent: *\nCrawl-delay: 12\nDisallow:\n'), (200, page())]):
            self.assertEqual(client.fetch(URL), page())
            self.assertEqual(client.interval, 12)
        client = b.BrowserClient(self.view, StubContext())
        with patch.object(client, '_read_browser', side_effect=[(404, b'not found'), (200, page())]):
            self.assertEqual(client.fetch(URL), page())

    def test_complete_flow_reopens_tls_page_and_preserves_original_error_event(self):
        self.store.put('continuation_detail', self.key, dict(status='error', error='SSL EOF'))
        self.store.event('continue_detail', 'DETAIL_ERROR', URL, 'SSL EOF')
        fixture = FixtureClient(self.view)
        with patch('builtins.print'):
            result = b.continuation.collect_known(self.view, fixture, [URL], limit=1)
        self.assertEqual(result['extracted'], 1)
        self.assertEqual(fixture.fetched, [URL])
        self.assertEqual(self.store.rows('report')[0]['evidence'], 'live_browser_response')
        self.assertEqual(self.store.db.execute('SELECT message FROM events WHERE code=?', ('DETAIL_ERROR',)).fetchone()[0], 'SSL EOF')


@unittest.skipUnless(os.environ.get('COLLECTOR_EDGE_TEST') == '1', 'set COLLECTOR_EDGE_TEST=1 for installed Edge smoke test')
class InstalledEdgeTests(unittest.TestCase):
    def test_actual_edge_fetch_and_core_zip_extraction_over_loopback(self):
        from playwright.sync_api import sync_playwright
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('项目/募投报告.pdf', b'%PDF-1.4\nsynthetic Edge fixture\n%%EOF\n')
        blob = buffer.getvalue()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                if self.path == '/robots.txt':
                    body, kind = b'User-agent: *\nDisallow:\n', 'text/plain'
                elif self.path == '/fxqgg/1.jhtml':
                    body = ('<h1>广东省2022年新增专项债券发行公开</h1>'
                            '<img src="/unwanted.png"><script src="/unwanted.js"></script>'
                            '<a href="/sample.zip">03珠海.zip</a>').encode()
                    kind = 'text/html; charset=utf-8'
                elif self.path == '/deny':
                    body, kind = b'<html>CloudWAF</html>', 'text/html'
                else:
                    body, kind = blob, 'application/zip'
                self.send_response(418 if self.path == '/deny' else 200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            store = c.Store(tmp)
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
                    try:
                        context = browser.new_context(java_script_enabled=False, service_workers='block')
                        client = b.BrowserClient(store, context, interval=0, test_mode=True, allowed_hosts={'127.0.0.1'})
                        origin = 'http://127.0.0.1:' + str(server.server_port)
                        url = origin + '/fxqgg/1.jhtml'
                        body = client.fetch(url)
                        c.ingest_detail(store, body, url, evidence='synthetic_test')
                        c.process(store, client, limit=1)
                        self.assertEqual(store.rows('attachment')[0]['status'], 'extracted')
                        self.assertEqual(len(store.rows('report')), 1)
                        self.assertNotIn('/unwanted.png', requests)
                        self.assertNotIn('/unwanted.js', requests)
                        with self.assertRaises(c.AccessStopped):
                            client.fetch(origin + '/deny')
                        self.assertTrue(store.get('host', '127.0.0.1:' + str(server.server_port))['blocked'])
                    finally:
                        browser.close()
            finally:
                store.db.close()
                server.shutdown()
                thread.join()
