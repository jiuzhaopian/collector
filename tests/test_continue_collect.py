import contextlib
import http.client
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector as c
import continue_collect as run


URL = "https://www.celma.org.cn/fxqgg/70584.jhtml"
NEXT = "https://www.celma.org.cn/fxqgg/57698.jhtml"


def page(count=1):
    return ('<h1>广东省2022年第一批新增专项债券发行公开</h1>' + ''.join(
        f'<a href="https://www.governbond.org.cn/uploadFiles/{i}.zip">珠海{i}.zip</a>'
        for i in range(count))).encode()


class FixtureClient:
    def __init__(self, store):
        self.store = store
        self.fetched, self.downloaded = [], []
        self.page_errors, self.zip_errors = {}, {}
        self.body = page()

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.page_errors:
            raise self.page_errors[url]
        return self.body

    def download(self, url):
        self.downloaded.append(url)
        if url in self.zip_errors:
            raise self.zip_errors[url]
        key = c.digest(url.encode())
        path = self.store.root / 'downloads' / (key + '.zip')
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('项目/募投报告.pdf', b'%PDF-1.4\nsynthetic fixture\n%%EOF\n')
        return dict(id=key, path=str(path.relative_to(self.store.root)),
                    kind='zip', sha256=c.file_hash(path))


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = c.Store(self.tmp.name)
        self.client = FixtureClient(self.store)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.store.db.close()
        self.tmp.cleanup()

    def ingest(self, url=URL, count=1):
        return c.ingest_detail(self.store, page(count), url, evidence='synthetic_test')

    def test_existing_19_successes_and_6_errors_skip_to_next_announcement(self):
        self.ingest(count=25)
        for i, a in enumerate(self.store.rows('attachment')):
            a.update(status='extracted' if i < 19 else 'error', error='original evidence')
            self.store.put('attachment', a['id'], a)
        before = self.store.rows('attachment')
        self.store.put('member', 'old-review', dict(id='old-review', status='review', reason='Invalid PDF signature or EOF'))
        stats = run.collect_known(self.store, self.client, [URL, NEXT], limit=1)
        self.assertEqual(self.client.fetched, [NEXT])
        self.assertEqual(len(self.client.downloaded), 1)
        self.assertEqual((stats['skipped_completed'], stats['skipped_failed'], stats['extracted']), (19, 6, 1))
        for old in before:
            self.assertEqual(self.store.get('attachment', old['id']), old)
        self.assertEqual(self.store.get('member', 'old-review')['status'], 'review')

    def test_network_error_continues_and_is_not_retried(self):
        self.ingest(count=2)
        rows = sorted(self.store.rows('attachment'), key=lambda a: a['name'])
        self.client.zip_errors[rows[0]['url']] = OSError('DNS failed')
        stats = run.collect_known(self.store, self.client, [URL], limit=2)
        self.assertEqual((stats['failed'], stats['extracted']), (1, 1))
        reports = self.store.rows('report')
        stats = run.collect_known(self.store, self.client, [URL], limit=2)
        self.assertEqual(len(self.client.downloaded), 2)
        self.assertEqual(stats['attachments_attempted'], 0)
        self.assertEqual(self.store.rows('report'), reports)

    def test_page_error_skipped_persistently_without_blocking_later_pages(self):
        self.client.page_errors[URL] = OSError('DNS failed')
        stats = run.collect_known(self.store, self.client, [URL, NEXT], limit=1)
        self.assertEqual((stats['detail_errors'], stats['extracted']), (1, 1))
        stats = run.collect_known(self.store, self.client, [URL, NEXT], limit=1)
        self.assertEqual(self.client.fetched.count(URL), 1)
        self.assertEqual(stats['skipped_detail_errors'], 1)

    def test_access_stop_is_not_a_skippable_error(self):
        self.client.page_errors[URL] = c.AccessStopped('CloudWAF 418')
        with self.assertRaises(c.AccessStopped):
            run.collect_known(self.store, self.client, [URL, NEXT])
        self.assertEqual(self.client.fetched, [URL])
        self.assertIsNone(self.store.get('continuation_detail', c.digest(URL.encode())))

    def test_download_access_stop_remains_blocked_on_next_run(self):
        self.ingest(count=2)
        first = sorted(self.store.rows('attachment'), key=lambda a: a['name'])[0]
        self.client.zip_errors[first['url']] = c.AccessStopped('429')
        for _ in range(2):
            with self.assertRaises(c.AccessStopped):
                run.collect_known(self.store, self.client, [URL, NEXT])
        self.assertEqual(len(self.client.downloaded), 1)
        self.assertEqual(self.client.fetched, [])
        self.assertEqual(self.store.get('attachment', first['id'])['status'], 'blocked')

    def test_budget_resumes_pending_zip_without_repeating_success(self):
        self.ingest(count=3)
        for expected in (1, 2, 3):
            stats = run.collect_known(self.store, self.client, [URL], limit=1)
            self.assertEqual(stats['attachments_attempted'], 1)
            self.assertEqual(len(self.client.downloaded), expected)
        self.assertEqual(len(set(self.client.downloaded)), 3)

    def test_unbounded_mode_keeps_real_sample_requirement(self):
        self.ingest()
        with self.assertRaises(c.AccessStopped):
            run.collect_known(self.store, self.client, [URL], limit=0)
        self.assertEqual(self.client.downloaded, [])

    def test_http_stream_failure_keeps_partial_file_and_continues(self):
        self.ingest(count=2)
        first = sorted(self.store.rows('attachment'), key=lambda a: a['name'])[0]
        part = self.store.root / 'downloads' / 'existing.part'
        part.parent.mkdir(exist_ok=True)
        part.write_bytes(b'partial')
        self.client.zip_errors[first['url']] = http.client.IncompleteRead(b'partial', 100)
        stats = run.collect_known(self.store, self.client, [URL], limit=2)
        self.assertEqual((stats['failed'], stats['extracted']), (1, 1))
        self.assertEqual(part.read_bytes(), b'partial')

    def test_url_list_rejects_foreign_or_non_detail_urls_and_deduplicates(self):
        self.assertEqual(run.detail_urls(['\ufeff' + URL, '# comment', '', URL]), [URL])
        for url in ['https://example.org/fxqgg/1.jhtml', 'http://www.celma.org.cn/fxqgg/1.jhtml',
                    'https://www.celma.org.cn/fxjg/1.jhtml']:
            with self.assertRaises(ValueError):
                run.detail_urls([url])


if __name__ == '__main__':
    unittest.main()
