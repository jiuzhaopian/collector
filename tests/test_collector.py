import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector as c


def pdf(text=b"sample"):
    # Synthetic signature/EOF fixture, not a downloaded or fully rendered document.
    return b"%PDF-1.4\n" + text + b"\n%%EOF\n"


def archive(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            z.writestr(name, content)
    return b.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    blob = archive([("项目/募投报告.pdf", pdf())])
    robots = b"User-agent: *\nDisallow:\n"

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests.append((self.path, dict(self.headers)))
        status, headers, body = 200, {}, self.blob
        if self.path == "/robots.txt":
            body = self.robots
        elif self.path == "/deny":
            status, body = 418, b"CloudWAF"
        elif self.path == "/rate":
            status, headers, body = 429, {"Retry-After": "120"}, b"limited"
        elif self.path == "/challenge":
            body = b"<html><title>Access Denied</title>CloudWAF</html>"
        elif self.path == "/redirect":
            status, headers, body = 302, {"Location": "https://unapproved.invalid/blob.zip"}, b""
        elif self.path == "/short":
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:40])
            self.wfile.flush()
            self.close_connection = True
            return
        elif self.path == "/html":
            body = b"<html>captcha</html>"
        elif self.path == "/badzip":
            body = b"PK broken"
        elif self.path == "/badpdf":
            body = b"%PDF-1.7 incomplete"
        elif self.path == "/416":
            status, headers, body = 416, {"Content-Range": "bytes */100"}, b""
        elif self.path in ("/range", "/wrong-range", "/changed-206") and self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
            body = body[start:]
            status = 206
            actual_start = start + 1 if self.path == "/wrong-range" else start
            headers["Content-Range"] = f"bytes {actual_start}-{len(self.blob)-1}/{len(self.blob)}"
        headers["ETag"] = '"v2"' if self.path in ("/changed", "/changed-206") else '"v1"'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = c.Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def detail(self, title="2024年月广东省2024年第四批新增专项债券发行公开", attachments=None):
        attachments = attachments or [("03-珠海.zip", "/uploadFiles/z.zip")]
        html = f"<h1>{title}</h1><div>2025 年 05 月 23 日</div>" + "".join(
            f'<a href="{url}">{name}</a>' for name, url in attachments)
        return c.ingest_detail(self.store, html.encode(), "https://www.celma.org.cn/fxqgg/1.jhtml", evidence="synthetic_test")

    def test_year_from_title_separate_publication(self):
        a = self.detail()
        self.assertEqual((a["year"], a["published_date"], a["status"]), (2024, "2025-05-23", "eligible"))

    def test_eligibility_scope(self):
        cases = [("广东省2022年新增专项债券", "eligible"), ("广东省2026年新增专项债券", "eligible"),
                 ("广东省2021年新增专项债券", "excluded"), ("广东省2024年再融资专项债券", "excluded"),
                 ("广东省2024年新增一般债券", "excluded"), ("深圳市2025年新增专项及再融资债券", "review"),
                 ("广东省2024年2025年新增专项债券", "review")]
        for title, expected in cases:
            self.assertEqual(c.eligibility(title, "https://www.celma.org.cn/fxqgg/1.jhtml")["status"], expected)
        self.assertEqual(c.eligibility("广东省2024年新增专项债券", "https://www.celma.org.cn/fxjg/1.jhtml")["status"], "excluded")

    def test_future_publication_review(self):
        body = "<h1>深圳市2026年新增专项债券</h1><p>2026年12月20日</p>".encode()
        a = c.ingest_detail(self.store, body, "https://www.celma.org.cn/fxqgg/2.jhtml")
        self.assertEqual(a["reason"], "published_after_as_of")

    def test_city_parts_exclusion_dedup(self):
        files = [("广州1.zip", "/uploadFiles/gz.zip"), ("11-东莞2.zip", "/uploadFiles/dg2.zip"),
                 ("11-东莞1.zip", "/uploadFiles/dg1.zip"), ("05佛山.zip", "/uploadFiles/fs.zip"),
                 ("05佛山.zip", "/uploadFiles/fs.zip"), ("湛江.zip", "/uploadFiles/zj.zip")]
        self.detail(attachments=files)
        rows = self.store.rows("attachment")
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(a["decision"] == "selected" for a in rows), 3)
        self.assertFalse(any(a["city"] == "广州" and a["decision"] == "selected" for a in rows))

    def test_shenzhen_district_zip(self):
        self.detail("2025年深圳市第五批专项债新增专项债券发行公开", [
            ("附件4-1：一案两书（市本级）.zip", "/uploadFiles/sz.zip"),
            ("附件4-9：一案两书（大鹏 新区）.zip", "/uploadFiles/dp.zip"),
            ("专项债券信用评级报告.zip", "/uploadFiles/rating.zip")])
        self.assertEqual(sum(a["decision"] == "selected" for a in self.store.rows("attachment")), 2)

    def test_early_shenzhen_disclosure_zip_selected(self):
        self.assertEqual(c.attachment_decision("债券信息披露文件（之一）.zip", "深圳", "https://www.governbond.org.cn/uploadFiles/a.zip")[0], "selected")

    def test_unassigned_guangdong_archive_requires_review(self):
        self.assertEqual(c.attachment_decision("项目文件.zip", "广东", "https://www.governbond.org.cn/uploadFiles/a.zip")[0], "review")

    def test_full_gate_rejects_without_real_sample(self):
        with self.assertRaisesRegex(c.AccessStopped, "successful real report sample"):
            c.process(self.store, None, "广东", None)

    def test_relative_encoded_onclick_links(self):
        page = c.Page('<a href="javascript:void(0)" onclick="window.open(\'/uploadFiles/%E7%8F%A0%E6%B5%B7.zip\')">珠海.zip</a>')
        rows = list(c.attachment_links(page, "https://www.celma.org.cn/fxqgg/1.jhtml"))
        self.assertEqual(len(rows), 1)
        self.assertIn("%E7", rows[0][1])

    def test_generic_tooltip_preserves_visible_filename(self):
        page = c.Page('<a title="点击下载" href="/uploadFiles/x.zip">05-佛山.zip</a>')
        self.assertEqual(list(c.attachment_links(page, "https://www.celma.org.cn/fxqgg/1.jhtml"))[0][0], "05-佛山.zip")

    def test_gb18030_html(self):
        text = '<h1>2023年广东省新增专项债券</h1><a href="/uploadFiles/x.zip">中山.zip</a>'
        a = c.ingest_detail(self.store, text.encode('gb18030'), 'https://www.celma.org.cn/fxqgg/3.jhtml')
        self.assertEqual(a['year'], 2023)
        self.assertEqual(self.store.rows('attachment')[0]['city'], '中山')

    def test_nested_zip_report_hash_and_provenance(self):
        self.detail()
        att = self.store.rows("attachment")[0]
        content = pdf()
        nested = archive([("项目/募集说明书.PDF", content)])
        blob = archive([("项目1/募投报告.pdf", content), ("项目1/法律意见书.pdf", pdf(b"legal")),
                        ("内部.zip", nested), ("../募投报告.pdf", content), ("广州/募投报告.pdf", content)])
        path = self.store.root / "archive.zip"
        path.write_bytes(blob)
        record = dict(path="archive.zip", kind="zip", sha256=c.digest(blob))
        self.assertEqual(c.extract_reports(self.store, att, record), 2)
        reports = self.store.rows("report")
        self.assertEqual(len({r["sha256"] for r in reports}), 1)
        self.assertEqual(len(list((self.store.root / "reports").glob("*.pdf"))), 1)
        self.assertTrue(any(m["reason"] == "unsafe_archive_path" for m in self.store.rows("member")))
        self.assertTrue(all(r["announcement_url"] and r["archive_sha256"] for r in reports))
        self.assertEqual(c.extract_reports(self.store, att, record), 2)
        self.assertEqual(len(self.store.rows("report")), 2)

    def test_no_match_is_exception(self):
        self.detail()
        att = self.store.rows("attachment")[0]
        (self.store.root / "archive.zip").write_bytes(archive([("实施方案.pdf", pdf())]))
        self.assertEqual(c.extract_reports(self.store, att, dict(path="archive.zip", kind="zip", sha256="x")), 0)
        self.assertEqual(self.store.db.execute("SELECT code FROM events").fetchone()[0], "NO_REPORT_MATCH")

    def test_import_downloaded_zip_without_network(self):
        self.detail()
        attachment = self.store.rows('attachment')[0]
        source = c.downloads_dir(self.store) / 'browser.zip'
        source.write_bytes(archive([('项目/募投报告.pdf',pdf())]))
        before = source.stat()
        self.assertEqual(c.import_file(self.store,source,attachment['url']),1)
        self.assertEqual(self.store.rows('attachment')[0]['status'],'extracted')
        self.assertEqual(self.store.rows('download')[0]['transport'],'imported_browser_download')
        self.assertEqual(c.import_file(self.store,source,attachment['url']),1)
        self.assertEqual(len(self.store.rows('report')),1)
        self.assertEqual(self.store.rows('download')[0]['path'],str(source.relative_to(self.store.root)))
        self.assertEqual(self.store.rows('download')[0]['storage'],'in_place')
        self.assertEqual(list(source.parent.iterdir()),[source])
        self.assertEqual(source.stat().st_ino,before.st_ino)
        self.assertEqual(source.stat().st_mtime_ns,before.st_mtime_ns)

    def test_import_requires_observed_source_link(self):
        with self.assertRaisesRegex(ValueError,'observed attachment'):
            c.import_file(self.store,'missing.zip','https://www.governbond.org.cn/uploadFiles/x.zip')

    def test_import_refuses_changed_attachment(self):
        self.detail()
        attachment = self.store.rows('attachment')[0]
        source=c.downloads_dir(self.store)/'browser.zip'
        source.write_bytes(archive([('募投报告.pdf',pdf(b'first'))]))
        c.import_file(self.store,source,attachment['url'])
        source.write_bytes(archive([('募投报告.pdf',pdf(b'changed'))]))
        with self.assertRaisesRegex(ValueError,'different content'):
            c.import_file(self.store,source,attachment['url'])

    def test_external_import_rejected_without_copy_or_move(self):
        self.detail()
        attachment=self.store.rows('attachment')[0]
        source=self.store.root/'external.zip'
        content=archive([('募投报告.pdf',pdf())])
        source.write_bytes(content)
        with self.assertRaisesRegex(ValueError,'Save the attachment directly inside'):
            c.import_file(self.store,source,attachment['url'])
        self.assertEqual(source.read_bytes(),content)
        self.assertEqual(list((self.store.root/'downloads').iterdir()),[])
        self.assertFalse(self.store.rows('download'))

    def test_temporary_browser_file_not_registered(self):
        self.detail()
        source=c.downloads_dir(self.store)/'browser.zip.crdownload'
        source.write_bytes(archive([('募投报告.pdf',pdf())]))
        with self.assertRaisesRegex(ValueError,'temporary downloads'):
            c.import_file(self.store,source,self.store.rows('attachment')[0]['url'])
        self.assertFalse(self.store.rows('download'))

    def test_same_url_second_path_does_not_replace_registered_file(self):
        self.detail()
        attachment=self.store.rows('attachment')[0]
        folder=c.downloads_dir(self.store)
        first=folder/'browser.zip'
        first.write_bytes(archive([('募投报告.pdf',pdf())]))
        c.import_file(self.store,first,attachment['url'])
        second=folder/'browser (1).zip'
        second.write_bytes(first.read_bytes())
        with self.assertRaisesRegex(ValueError,'already registered'):
            c.import_file(self.store,second,attachment['url'])
        self.assertEqual(self.store.rows('download')[0]['path'],str(first.relative_to(self.store.root)))
        self.assertEqual(set(folder.iterdir()),{first,second})

    def test_download_dir_cli_is_portable_and_offline(self):
        import contextlib
        output=io.StringIO()
        data=self.store.root/'deployment data'
        with contextlib.redirect_stdout(output):
            self.assertEqual(c.main(['--data',str(data),'download-dir']),0)
        self.assertEqual(Path(output.getvalue().strip()),data/'downloads')
        self.assertTrue((data/'downloads').is_dir())
        self.assertFalse((data/'state.sqlite').exists())

    def test_path_guards_and_csv_injection(self):
        for name in ("../x", "/x", "C:\\x", "a/../../x"):
            self.assertFalse(c.safe_member(name))
        self.assertTrue(c.safe_member("项目/募投报告.pdf"))
        self.assertEqual(c.csv_safe(" =HYPERLINK(1)"), "' =HYPERLINK(1)")

    def test_export_empty_is_unknown_not_complete(self):
        out = c.export_data(self.store)
        data = json.loads((out / "index.json").read_text(encoding="utf-8"))
        self.assertFalse(data["summary"]["complete"])
        self.assertEqual(len(data["coverage"]), 40)
        self.assertTrue(all(r["coverage"] == "unknown_not_reconciled" for r in data["coverage"]))

    def test_success_events_not_exported_as_exceptions(self):
        for code in ['STRUCTURE','LOCAL_ATTACHMENT_VERIFIED','REAL_BROWSER_SAMPLE_PASSED']:
            self.store.event('sample',code,'','success')
        out=c.export_data(self.store)
        data=json.loads((out/'index.json').read_text(encoding='utf-8'))
        self.assertEqual(data['summary']['exceptions'],0)

    def test_har_only_public_bodies_no_auth_replay(self):
        body = "<h1>2025年深圳市新增专项债券</h1><a href='/uploadFiles/x.zip'>一案两书.zip</a>"
        har = {"log": {"entries": [{"request": {"url": "https://www.celma.org.cn/fxqgg/5.jhtml", "headers": [{"name": "Cookie", "value": "secret"}]},
                "response": {"status": 200, "content": {"text": body}}}]}}
        path = self.store.root / "input.har"
        path.write_text(json.dumps(har), encoding="utf-8")
        self.assertEqual(c.import_har(self.store, path, "2026-10-16"), 1)
        raw = list((self.store.root / "raw").glob("*.html"))[0].read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)

    def test_dynamic_list_explicit_stop(self):
        class Fake:
            def fetch(self, url):
                return b'<script src="list.js"></script><div id="table"></div>'
        with self.assertRaises(c.AccessStopped):
            c.discover(self.store, Fake(), "https://www.celma.org.cn/fxqgg/index.jhtml")

    def test_static_discovery_observed_pagination(self):
        class Fake:
            def fetch(self, url):
                return ('<a href="/fxqgg/1.jhtml">2024年广东省新增专项债券</a>' +
                        ('<a rel="next" href="/fxqgg/index_2.jhtml">下一页</a>' if "index.jhtml" in url else "")).encode()
        result = c.discover(self.store, Fake(), "https://www.celma.org.cn/fxqgg/index.jhtml")
        self.assertEqual(len(result), 1)
        self.assertEqual(len(self.store.rows("list_page")), 2)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = c.Store(self.tmp.name)
        Handler.requests, Handler.robots = [], b"User-agent: *\nDisallow:\n"
        self.client = c.Client(self.store, interval=0, test_mode=True, allowed_hosts={"127.0.0.1"})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def seed_partial(self, endpoint, etag='"v1"'):
        url = self.base + endpoint
        folder = self.store.root / "downloads"
        folder.mkdir()
        key = c.digest(url.encode())
        (folder / (key + ".part")).write_bytes(Handler.blob[:40])
        c.atomic_json(folder / (key + ".part.json"), dict(etag=etag, url=url))
        return url

    def test_range_resume_real_http(self):
        url = self.seed_partial("/range")
        r = self.client.download(url)
        self.assertEqual(r["sha256"], c.digest(Handler.blob))
        headers = Handler.requests[-1][1]
        self.assertEqual(headers.get("Range"), "bytes=40-")
        self.assertEqual(headers.get("If-Range"), '"v1"')

    def test_complete_pipeline_over_loopback(self):
        url = "https://www.celma.org.cn/fxqgg/1.jhtml"
        html = f'<h1>2024年广东省新增专项债券</h1><p>2024年06月05日</p><a href="{self.base}/range">03-珠海.zip</a>'
        c.ingest_detail(self.store, html.encode(), url, evidence="synthetic_test")
        self.assertEqual(c.process(self.store, self.client, "广东", 1), 1)
        self.assertEqual(self.store.rows("attachment")[0]["status"], "extracted")
        self.assertEqual(len(self.store.rows("report")), 1)
        out = c.export_data(self.store)
        data = json.loads((out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["unique_pdfs"], 1)
        self.assertFalse(data["summary"]["complete"])

    def test_ignore_range_restarts_without_appending(self):
        url = self.seed_partial("/ignore")
        r = self.client.download(url)
        self.assertEqual(r["bytes"], len(Handler.blob))
        self.assertEqual(r["sha256"], c.digest(Handler.blob))

    def test_changed_etag_200_replaces(self):
        r = self.client.download(self.seed_partial("/changed"))
        self.assertEqual(r["sha256"], c.digest(Handler.blob))

    def test_wrong_content_range_never_appends(self):
        url = self.seed_partial("/wrong-range")
        with self.assertRaisesRegex(ValueError, "Content-Range"):
            self.client.download(url)
        partial = next((self.store.root / "downloads").glob("*.part"))
        self.assertEqual(partial.stat().st_size, 40)

    def test_changed_etag_206_rejected(self):
        with self.assertRaisesRegex(ValueError, "ETag changed"):
            self.client.download(self.seed_partial("/changed-206"))

    def test_missing_validator_restarts(self):
        url = self.seed_partial("/range", etag=None)
        self.client.download(url)
        self.assertNotIn("Range", Handler.requests[-1][1])

    def test_short_read_retains_partial(self):
        with self.assertRaises(ValueError):
            self.client.download(self.base + "/short")
        self.assertEqual(len(self.store.rows("download")), 0)
        self.assertTrue(next((self.store.root / "downloads").glob("*.part")).exists())

    def test_416_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "416"):
            self.client.download(self.seed_partial("/416"))
        self.assertFalse(self.store.rows("download"))

    def test_block_circuit_persisted_no_retries(self):
        with self.assertRaises(c.AccessStopped):
            self.client.fetch(self.base + "/deny")
        count = len(Handler.requests)
        client2 = c.Client(self.store, interval=0, test_mode=True, allowed_hosts={"127.0.0.1"})
        with self.assertRaises(c.AccessStopped):
            client2.fetch(self.base + "/range")
        self.assertEqual(len(Handler.requests), count)

    def test_429_does_not_retry(self):
        with self.assertRaises(c.AccessStopped):
            self.client.fetch(self.base + "/rate")
        self.assertEqual(sum(p == "/rate" for p, _ in Handler.requests), 1)

    def test_robots_denied_prevents_target_request(self):
        Handler.robots = b"User-agent: *\nDisallow: /range\nCrawl-delay: 5\n"
        with self.assertRaises(c.AccessStopped):
            self.client.fetch(self.base + "/range")
        self.assertEqual([p for p, _ in Handler.requests], ["/robots.txt"])
        self.assertEqual(self.client.interval, 5)

    def test_redirect_host_guard(self):
        with self.assertRaises(c.AccessStopped):
            self.client.fetch(self.base + "/redirect")

    def test_html_challenge_cannot_be_zip(self):
        with self.assertRaises(c.AccessStopped):
            self.client.download(self.base + "/html")
        self.assertFalse(self.store.rows("download"))

    def test_bad_zip_and_pdf_rejected(self):
        for endpoint in ("/badzip", "/badpdf"):
            with self.assertRaises((ValueError, zipfile.BadZipFile)):
                self.client.download(self.base + endpoint)
        self.assertFalse(self.store.rows("download"))

    def test_cached_file_hash_checked(self):
        url = self.base + "/range"
        r = self.client.download(url)
        count = len(Handler.requests)
        self.client.download(url)
        self.assertEqual(len(Handler.requests), count)
        (self.store.root / r["path"]).write_bytes(b"tampered")
        r2 = self.client.download(url)
        self.assertEqual(r2["sha256"], c.digest(Handler.blob))
        self.assertGreater(len(Handler.requests), count)

    def test_downloader_reuses_browser_file_without_new_download(self):
        url=self.base+'/browser.zip'
        html=f'<h1>2024年广东省新增专项债券</h1><a href="{url}">珠海.zip</a>'
        c.ingest_detail(self.store,html.encode(),'https://www.celma.org.cn/fxqgg/1.jhtml',evidence='synthetic_test')
        source=c.downloads_dir(self.store)/'original_browser_name.zip'
        source.write_bytes(Handler.blob)
        c.import_file(self.store,source,url)
        result=self.client.download(url)
        self.assertEqual(Handler.requests,[])
        self.assertEqual(result['path'],str(source.relative_to(self.store.root)))
        self.assertEqual(list(source.parent.iterdir()),[source])

    def test_direct_http_download_writes_only_one_zip(self):
        url=self.base+'/direct.zip'
        result=self.client.download(url)
        target=self.store.root/result['path']
        self.assertEqual(target.parent,self.store.root/'downloads')
        self.assertEqual(target.suffix,'.zip')
        self.assertEqual(target.read_bytes(),Handler.blob)
        self.assertFalse(list(target.parent.glob('*.bin')))
        self.assertFalse(list(target.parent.glob('*.part')))
        count=len(Handler.requests)
        self.client.download(url)
        self.assertEqual(len(Handler.requests),count)
        self.assertEqual(list(target.parent.glob('*.zip')),[target])

    def test_registered_path_cannot_escape_download_folder(self):
        url=self.base+'/outside.zip'
        self.store.put('download',c.digest(url.encode()),dict(path='../outside.zip',sha256='invalid'))
        with self.assertRaisesRegex(ValueError,'outside data/downloads'):
            self.client.download(url)
        self.assertEqual(Handler.requests,[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
