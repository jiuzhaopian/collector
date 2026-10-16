"""One-click Edge announcement reader, followed by the existing ZIP pipeline.

Scope: observed Guangdong URLs only. Browser transport does not override saved
access restrictions. Old HTTP TLS page failures get one browser attempt; failed
ZIPs and reviewed members remain skipped. No personal browser profile is used.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import re
import subprocess
import sys
import time
import urllib.robotparser
from pathlib import Path

import collector as c
import continue_collect as continuation


PLAYWRIGHT_VERSION = "1.64.0"


class BrowserStore:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get(self, kind, key):
        row = self.store.get(kind, key)
        if (kind == "continuation_detail" and row and row.get("status") == "error"
                and re.search(r"SSL|_ssl|violation of protocol", row.get("error", ""), re.I)
                and not self.store.get("browser_detail_attempt", key)):
            return None  # Different transport, once; keep the original audit event.
        return row

    def put(self, kind, key, row):
        if kind in ("announcement", "attachment") and row.get("evidence") == "live_http":
            row = dict(row, evidence="live_browser_response")
        return self.store.put(kind, key, row)


def challenge(body):
    return bool(re.search(br"CloudWAF|captcha|<title[^>]*>[^<]*(?:Access Denied|security check)", body, re.I)
                or any(t in body.decode("utf-8", "ignore") for t in ("访问被拦截", "访问被拒绝", "人机验证")))


class BrowserClient(c.Client):
    """Only detail-page reads use Edge. Downloads retain the tested HTTP client."""
    def __init__(self, store, context, interval=4, test_mode=False, allowed_hosts=None):
        super().__init__(store, interval, test_mode=test_mode, allowed_hosts=allowed_hosts)
        self.page = context.new_page()
        self.page.set_default_navigation_timeout(45000)
        self.page.route("**/*", self._route)
        self.browser_robots = {}
        self.navigation_origin = None
        self.route_error = None

    def _route(self, route):
        request = route.request
        if not request.is_navigation_request() or request.frame != self.page.main_frame:
            route.abort()
            return
        try:
            parsed = self.validate(request.url)
            origin = parsed.scheme + "://" + parsed.netloc
            if origin != self.navigation_origin:
                self.stop(request.url, "Browser redirect outside the requested origin")
            parser = self.browser_robots.get(origin)
            if parser and not parser.can_fetch(c.USER_AGENT, request.url):
                self.stop(request.url, "Disallowed by robots.txt")
            delay = self.interval - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()
        except c.AccessStopped as e:
            self.route_error = e
            route.abort()
            return
        route.continue_()

    def _read_browser(self, url, limit):
        parsed = self.validate(url)
        self.navigation_origin = parsed.scheme + "://" + parsed.netloc
        self.route_error = None
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
            if self.route_error:
                raise self.route_error
            if response is None:
                raise OSError("Browser returned no document response")
            status = response.status
            if status in c.BLOCK_CODES:
                self.stop(url, "Browser HTTP " + str(status) + "; no automatic retry")
            body = response.body()
            if len(body) > limit:
                raise ValueError("Browser document exceeds size limit")
            if challenge(body):
                self.store.raw(body)
                self.stop(url, "Browser access challenge; no automatic retry")
            return status, body
        except (c.AccessStopped, ValueError, OSError):
            raise
        except Exception as e:
            # Playwright's timeout/connection errors are ordinary page failures.
            if self.route_error:
                raise self.route_error
            raise OSError("Browser navigation failed: " + str(e)) from e

    def fetch(self, url, limit=16 * 1024**2):
        parsed = self.validate(url)
        origin = parsed.scheme + "://" + parsed.netloc
        self.store.put("browser_detail_attempt", c.digest(url.encode()), dict(url=url, at=c.now()))
        if origin not in self.browser_robots:
            print("[Edge robots] " + origin + "/robots.txt", flush=True)
            try:
                status, body = self._read_browser(origin + "/robots.txt", 1024**2)
            except OSError as e:
                self.stop(url, "Cannot confirm robots.txt through browser: " + str(e))
            self.store.raw(body, "robots.txt")
            if status == 404:
                text = "User-agent: *\nDisallow:\n"
            elif status == 200 and not re.search(br"<html|<script", body, re.I):
                text = body.decode("utf-8-sig")
            else:
                self.stop(url, "Browser robots.txt unavailable/invalid: HTTP " + str(status))
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            self.browser_robots[origin] = parser
            delay = parser.crawl_delay(c.USER_AGENT) or parser.crawl_delay("*") or 0
            rate = parser.request_rate(c.USER_AGENT) or parser.request_rate("*")
            self.interval = max(self.interval, delay, rate.seconds / rate.requests if rate else 0)
        if not self.browser_robots[origin].can_fetch(c.USER_AGENT, url):
            self.stop(url, "Disallowed by robots.txt")
        print("[Edge page] " + url, flush=True)
        status, body = self._read_browser(url, limit)
        if status != 200:
            raise ValueError("Browser HTTP " + str(status))
        return body


def ensure_browser_library(root):
    runtime = Path(root).resolve() / "browser-runtime"
    sys.path.insert(0, str(runtime))
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        wheels = Path(__file__).resolve().parent / "browser-wheels"
        source = ["--no-index", "--find-links", str(wheels)] if wheels.is_dir() else []
        print("首次启动：" + ("从附带安装包离线安装浏览器控制组件。" if source else
                           "联网安装浏览器控制组件，请保持网络连接。"), flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "--retries", "1", "--timeout", "30", *source,
                        "--target", str(runtime), "playwright==" + PLAYWRIGHT_VERSION], check=True)
        importlib.invalidate_caches()
        return importlib.import_module("playwright.sync_api")


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, value):
        self.console.write(value)
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self):
        self.console.flush()
        self.log.flush()


def run(args, store):
    if args.limit == 0 and not any(r.get("region") == "广东" and r.get("evidence") != "synthetic_test"
                                  for r in store.rows("report")):
        raise ValueError("首次采集请先运行 --limit 1；已有真实成功样本后可不限制附件数量。")
    urls = continuation.detail_urls(Path(args.urls_file).read_text(encoding="utf-8-sig").splitlines()
                                    if args.urls_file else continuation.GUANGDONG_URLS)
    print("自动读取已确认的广东公告，广州排除；成功与失败附件均跳过。", flush=True)
    print("范围为已观察公告清单，不是五年完整清单。遇到访问限制会停止。", flush=True)
    api = ensure_browser_library(store.root)
    with api.sync_playwright() as p:
        # Uses the installed Edge and an isolated temporary context. No stealth,
        # personal cookies, certificate overrides, proxy rotation or CAPTCHA work.
        browser = p.chromium.launch(channel="msedge", headless=False, chromium_sandbox=True)
        try:
            context = browser.new_context(ignore_https_errors=False, accept_downloads=False,
                                          java_script_enabled=False, service_workers="block")
            view = BrowserStore(store)
            return continuation.collect_known(view, BrowserClient(view, context, args.interval),
                                              urls, limit=args.limit, as_of=args.as_of)
        finally:
            browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--interval", type=float, default=4)
    parser.add_argument("--as-of", default="2026-10-16")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--urls-file")
    args = parser.parse_args(argv)
    c.date.fromisoformat(args.as_of)
    if args.limit < 0 or args.interval < 3:
        parser.error("limit must be nonnegative; interval must be at least 3 seconds")
    store = c.Store(args.data)
    scope = store.get("settings", "scope")
    if scope and scope.get("as_of") != args.as_of:
        store.db.close()
        parser.error("Use the existing snapshot cutoff for this data directory")
    store.put("settings", "scope", dict(as_of=args.as_of))
    log_dir = store.root / "logs"
    log_dir.mkdir(exist_ok=True)
    log = log_dir / ("browser-" + c.now().replace(":", "-") + ".log")
    code = 0
    with log.open("x", encoding="utf-8") as stream, contextlib.redirect_stdout(Tee(sys.stdout, stream)), contextlib.redirect_stderr(Tee(sys.stderr, stream)):
        print("日志：" + str(log), flush=True)
        try:
            run(args, store)
        except KeyboardInterrupt:
            print("已中断，已完成结果保留。", flush=True)
            code = 130
        except Exception as e:
            store.event("browser_collect", "RUN_STOPPED", "", str(e))
            print("采集停止：" + str(e), flush=True)
            code = 2
        finally:
            try:
                c.export_data(store)
            finally:
                store.db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
