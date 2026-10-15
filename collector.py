"""CELMA collection pipeline. Python 3.11+, standard library only.

No guessed API endpoints, authentication, CAPTCHA solving or WAF workarounds.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zipfile
from datetime import datetime, timezone, date
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

VERSION = "0.2.1"
USER_AGENT = "CelmaResearchCollector/0.1 (public bond research; single worker)"
HOSTS = {"www.celma.org.cn", "www.governbond.org.cn"}
CITIES = ("珠海", "佛山", "惠州", "东莞", "中山", "江门", "肇庆")
DISTRICTS = ("市本级", "罗湖区", "福田区", "南山区", "盐田区", "宝安区", "龙岗区", "龙华区", "坪山区", "光明区", "大鹏新区", "前海管理局", "深汕特别合作区")
BLOCK_CODES = {401, 403, 418, 429}
PDF_RE = re.compile(r"募投|募集说明书|募集资金投向|募集资金投资")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def canonical(url, base=""):
    u = urllib.parse.urlsplit(urllib.parse.urljoin(base, url.strip()))
    if u.scheme not in ("https", "http") or u.username or u.password:
        raise ValueError("Unsupported or credential-bearing URL")
    # Preserve query order and parameters: signed attachment URLs must not change.
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "/", u.query, ""))


def encode_url(url):
    return urllib.parse.quote(url, safe=":/?&=%+;,@!$'()*~-._")


def decode_html(data):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("Unknown HTML encoding; preserve raw response for review")


class AccessStopped(RuntimeError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, body TEXT,
            PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT,
            stage TEXT, code TEXT, source TEXT, message TEXT);
        """)

    def put(self, kind, key, body):
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                        (kind, key, json.dumps(body, ensure_ascii=False)))
        self.db.commit()

    def get(self, kind, key):
        row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, kind):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT body FROM records WHERE kind=? ORDER BY id", (kind,))]

    def event(self, stage, code, source, message):
        record = dict(at=now(), stage=stage, code=code, source=source, message=str(message))
        self.db.execute("INSERT INTO events(at,stage,code,source,message) VALUES(:at,:stage,:code,:source,:message)", record)
        self.db.commit()
        with (self.root / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def raw(self, body, suffix="html"):
        path = self.root / "raw" / (digest(body) + "." + suffix)
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(body)
        return str(path.relative_to(self.root))


class RedirectGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Each redirect must go through its own host allowlist and robots check.
        return None


def downloads_dir(store):
    folder = (store.root / "downloads").resolve()
    if not folder.is_relative_to(store.root):
        raise ValueError("Download directory resolves outside the data directory")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def registered_download_path(store, record):
    folder = downloads_dir(store)
    path = (store.root / record["path"]).resolve()
    if not path.is_relative_to(folder):
        raise ValueError("Registered attachment path is outside data/downloads")
    return path


class Client:
    def __init__(self, store, interval=4.0, timeout=40, max_bytes=4 * 1024**3,
                 allowed_hosts=None, test_mode=False):
        if interval < 3 and not test_mode:
            raise ValueError("Minimum request interval is 3 seconds")
        self.store, self.interval, self.timeout = store, interval, timeout
        self.max_bytes = max_bytes
        self.hosts = allowed_hosts or HOSTS
        self.opener = urllib.request.build_opener(RedirectGuard())
        self.robots = {}
        self.last = 0.0
        self.test_mode = test_mode

    def validate(self, url):
        u = urllib.parse.urlsplit(canonical(url))
        if u.hostname not in self.hosts:
            raise AccessStopped("Unapproved attachment host: " + str(u.hostname))
        if not self.test_mode and (u.scheme != "https" or u.port not in (None, 443)):
            raise AccessStopped("Only standard HTTPS is allowed")
        state = self.store.get("host", u.netloc) or {}
        if state.get("blocked"):
            raise AccessStopped("Host circuit open: " + u.netloc + "; " + state.get("reason", ""))
        return u

    def stop(self, url, reason):
        host = urllib.parse.urlsplit(url).netloc
        self.store.put("host", host, dict(host=host, blocked=True, reason=reason, at=now()))
        self.store.event("network", "ACCESS_STOPPED", url, reason)
        raise AccessStopped(reason)

    def low_request(self, url, headers=None):
        self.validate(url)
        delay = self.interval - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()
        req = urllib.request.Request(encode_url(url), headers={
            "User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})})
        try:
            result = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            result = e
        self.store.event("network", "HTTP", url, json.dumps(dict(
            status=result.status, content_type=result.headers.get("Content-Type"),
            content_length=result.headers.get("Content-Length"),
            retry_after=result.headers.get("Retry-After")), ensure_ascii=False))
        if result.status in BLOCK_CODES:
            evidence = result.read(256 * 1024)
            self.store.raw(evidence)
            result.close()
            self.stop(url, "HTTP " + str(result.status) + "; no automatic retry")
        return result

    def check_robots(self, url):
        u = self.validate(url)
        origin = u.scheme + "://" + u.netloc
        if origin not in self.robots:
            with self.low_request(origin + "/robots.txt") as r:
                body = r.read(1024 * 1024 + 1)
                self.store.raw(body, "robots.txt")
                if r.status == 404:
                    text = "User-agent: *\nDisallow:\n"
                elif r.status == 200 and len(body) <= 1024 * 1024 and not re.search(
                        b"<html|<script|captcha|CloudWAF", body, re.I):
                    text = body.decode("utf-8-sig")
                else:
                    self.stop(url, "robots.txt unavailable/invalid: HTTP " + str(r.status))
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            self.robots[origin] = parser
            crawl_delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0
            rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
            self.interval = max(self.interval, crawl_delay, rate.seconds / rate.requests if rate else 0)
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            self.stop(url, "Disallowed by robots.txt")

    def request(self, url, headers=None):
        url = canonical(url)
        for _ in range(6):
            self.check_robots(url)
            r = self.low_request(url, headers)
            if r.status in (301, 302, 303, 307, 308):
                location = r.headers.get("Location")
                r.close()
                if not location:
                    raise ValueError("Redirect without Location")
                url = canonical(location, url)
                continue
            return r
        raise ValueError("Too many redirects")

    def fetch(self, url, limit=16 * 1024**2):
        with self.request(url) as r:
            body = r.read(limit + 1)
            if r.status != 200:
                raise ValueError("HTTP " + str(r.status))
            if len(body) > limit:
                raise ValueError("Response exceeds HTML/JSON limit")
            if re.search(b"CloudWAF|<title[^>]*>[^<]*(?:captcha|Access Denied)", body, re.I) or "访问被拦截" in body.decode("utf-8", "ignore"):
                self.store.raw(body)
                self.stop(url, "Access challenge returned with HTTP 200")
            return body

    def download(self, url):
        key = digest(canonical(url).encode())
        folder = downloads_dir(self.store)
        old = self.store.get("download", key)
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        final = registered_download_path(self.store, old) if old else folder / (key + (suffix if suffix in (".zip", ".pdf") else ".bin"))
        partial = folder / (key + ".part")
        meta_path = folder / (key + ".part.json")
        if old and final.exists() and file_hash(final) == old.get("sha256"):
            return old
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        start = partial.stat().st_size if partial.exists() else 0
        validator = meta.get("etag") or meta.get("last_modified")
        if validator and validator.startswith("W/"):
            validator = meta.get("last_modified")
        headers = {"Referer": "https://www.celma.org.cn/"}
        if start and validator:
            headers.update({"Range": f"bytes={start}-", "If-Range": validator})
        else:
            start = 0
        with self.request(url, headers) as r:
            if r.status == 416:
                # Do not accept a partial as complete on a 416. Preserve it for audit.
                raise ValueError("Range 416; partial preserved, not marked complete")
            if r.status not in (200, 206):
                raise ValueError("Download HTTP " + str(r.status))
            if r.status == 200:
                start = 0  # Range ignored or If-Range changed: replace, never append.
            content_range = r.headers.get("Content-Range", "")
            total = None
            if r.status == 206:
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not m or int(m[1]) != start or int(m[2]) < start or int(m[2]) >= int(m[3]):
                    raise ValueError("Invalid Content-Range; refusing to append")
                total = int(m[3])
                expected = int(m[2]) - start + 1
                if start and meta.get("etag") and r.headers.get("ETag") and meta["etag"] != r.headers["ETag"]:
                    raise ValueError("ETag changed during partial response")
                if start and not meta.get("etag") and meta.get("last_modified") and r.headers.get("Last-Modified") and meta["last_modified"] != r.headers["Last-Modified"]:
                    raise ValueError("Last-Modified changed during partial response")
                if start and meta.get("total") is not None and meta["total"] != total:
                    raise ValueError("Remote total size changed during partial response")
            else:
                expected = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
                total = expected
            if total is not None and total > self.max_bytes:
                raise ValueError("Attachment exceeds configured byte limit")
            if r.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                raise ValueError("Encoded response cannot be safely range-resumed")
            meta = dict(url=url, etag=r.headers.get("ETag"), last_modified=r.headers.get("Last-Modified"), total=total)
            # Truncate before writing new metadata to avoid pairing old bytes/new ETag after interruption.
            with partial.open("ab" if start else "wb") as f:
                atomic_json(meta_path, meta)
                received = 0
                while True:
                    block = r.read(256 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if start + received > self.max_bytes:
                        raise ValueError("Attachment exceeds configured byte limit")
                    if not start and received == len(block) and not block.startswith((b"PK", b"%PDF-")):
                        if b"<html" in block.lower() or b"CloudWAF" in block or "访问被拦截" in block.decode("utf-8", "ignore"):
                            self.stop(url, "HTML/challenge returned as attachment")
                        raise ValueError("Unsupported attachment signature (ZIP/PDF required)")
                    f.write(block)
                f.flush()
                os.fsync(f.fileno())
            size = partial.stat().st_size
            if expected is not None and received != expected or total is not None and size != total:
                raise ValueError("Incomplete transfer; partial retained")
        # Validate all ZIP members' CRC before committing download success.
        with partial.open("rb") as f:
            signature = f.read(8)
        if signature.startswith(b"PK"):
            with zipfile.ZipFile(partial) as z:
                validate_zip(z)
                bad = z.testzip()
                if bad:
                    raise ValueError("ZIP CRC failed: " + bad)
            kind = "zip"
        elif signature.startswith(b"%PDF-"):
            if not pdf_valid(partial):
                raise ValueError("PDF missing EOF marker")
            kind = "pdf"
        else:
            raise ValueError("Unsupported file signature")
        os.replace(partial, final)
        record = dict(id=key, url=url, path=str(final.relative_to(self.store.root)),
                      bytes=size, sha256=file_hash(final), kind=kind, completed_at=now())
        self.store.put("download", key, record)
        return record


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links, self.scripts, self.text, self.headings = [], [], [], []
        self.a, self.heading, self.skip = None, None, 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        if tag == "a":
            self.a = dict(href=a.get("href", ""), title=a.get("title", ""),
                          rel=a.get("rel", ""), onclick=a.get("onclick", ""), text="")
        if tag in ("h1", "h2", "title"):
            self.heading = [tag, ""]

    def handle_endtag(self, tag):
        if tag == "a" and self.a is not None:
            self.links.append(self.a)
            self.a = None
        if self.heading and tag == self.heading[0]:
            self.headings.append(tuple(self.heading))
            self.heading = None
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self.skip:
            return
        self.text.append(data)
        if self.a is not None:
            self.a["text"] += data
        if self.heading is not None:
            self.heading[1] += data

    @property
    def title(self):
        for tag in ("h1", "h2", "title"):
            for t, text in self.headings:
                if t == tag:
                    return re.sub(r"\s+", "", text)
        return ""


def eligibility(title, url, as_of="2026-10-16"):
    text = re.sub(r"\s+", "", title)
    years = set(re.findall(r"(20\d{2})年", text))
    region = "深圳" if "深圳" in text else "广东" if "广东" in text else ""
    info = dict(title=title, url=url, region=region, year=int(next(iter(years))) if len(years) == 1 else None,
                status="excluded", reason="not_target")
    if not region:
        return info
    if len(years) != 1:
        info.update(status="review", reason="ambiguous_bond_year")
    elif not 2022 <= info["year"] <= 2026:
        info["reason"] = "year_out_of_scope"
    elif "/fxqgg/" not in urllib.parse.urlsplit(url).path:
        info["reason"] = "not_preissuance_category"
    elif "新增" in text and "再融资" in text:
        info.update(status="review", reason="mixed_new_refinancing")
    elif "再融资" in text or "置换" in text:
        info["reason"] = "refinancing_or_swap"
    elif "新增" in text and "专项" in text:
        info.update(status="eligible", reason="new_special_preissuance")
    else:
        info["reason"] = "not_explicit_new_special"
    return info


def attachment_decision(name, region, url):
    text = re.sub(r"\s+", "", urllib.parse.unquote(name))
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    zip_kind = text.lower().endswith(".zip") or suffix == ".zip"
    pdf_kind = text.lower().endswith(".pdf") or suffix == ".pdf"
    if "广州" in text:
        return "excluded", "", "guangzhou_excluded"
    if not (zip_kind or pdf_kind):
        return "review", "", "unsupported_attachment"
    if region == "广东":
        cities = [c for c in CITIES if c in text]
        if len(cities) > 1:
            return "review", "", "mixed_city_archive"
        if not cities:
            if any(k in text for k in ("汕头", "韶关", "河源", "梅州", "汕尾", "阳江", "湛江", "茂名", "清远", "潮州", "揭阳", "云浮", "省本级", "信用评级", "项目信息")):
                return "excluded", "", "not_target_city_or_nonreport"
            return ("review", "", "unassigned_archive") if zip_kind else ("excluded", "", "not_city_report_pdf")
        return ("selected", cities[0], "city_archive_or_report") if zip_kind or PDF_RE.search(text) else ("excluded", cities[0], "not_report_pdf")
    if region == "深圳":
        district = next((d for d in DISTRICTS if d in text), "待识别")
        if zip_kind and any(k in text for k in ("一案两书", "募投", "募集", "实施方案", "信息披露文件")) or pdf_kind and PDF_RE.search(text):
            return "selected", "深圳", district
        if zip_kind and not any(k in text for k in ("信用评级", "项目信息")):
            return "review", "深圳", "unknown_shenzhen_archive"
        return "excluded", "深圳", "not_project_report_archive"
    return "excluded", "", "not_target_region"


def attachment_links(page, base):
    for a in page.links:
        href = a["href"]
        if not href or href.lower().startswith(("javascript:", "#")):
            # Only literal observed href arguments, never execute JS.
            m = re.search(r"['\"]((?:https?://|/uploadFiles/)[^'\"]+)['\"]", a["onclick"])
            if not m:
                continue
            href = m[1]
        try:
            url = canonical(href, base)
        except ValueError:
            continue
        visible, tooltip = a["text"].strip(), a["title"].strip()
        name = tooltip if re.search(r"\.(zip|pdf|rar|7z)$", tooltip, re.I) else visible or tooltip
        if re.search(r"\.(?:zip|pdf|rar|7z)(?:$|[?&#])", urllib.parse.unquote(url), re.I) or re.search(r"\.(?:zip|pdf|rar|7z)\b", name, re.I):
            yield name or urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name), url


def ingest_detail(store, body, url, as_of="2026-10-16", title_hint="", evidence="live_http"):
    page = Page(decode_html(body))
    title = page.title or title_hint
    record = eligibility(title, url, as_of)
    record.update(id=digest(url.encode()), raw_path=store.raw(body), evidence=evidence, seen_at=now())
    text = " ".join(page.text)
    m = re.search(r"(20\d{2})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", text.replace(title, "", 1))
    record["published_date"] = ""
    if m:
        try:
            record["published_date"] = date(*map(int, m.groups())).isoformat()
        except ValueError:
            store.event("detail", "INVALID_DATE", url, m[0])
    if record["published_date"] and record["published_date"] > as_of:
        record.update(status="review", reason="published_after_as_of")
    record["batch_key"] = digest((str(record["year"]) + record["region"] + title).encode())
    store.put("announcement", record["id"], record)
    if record["status"] != "eligible":
        return record
    count = 0
    for name, attachment_url in attachment_links(page, url):
        status, city, reason = attachment_decision(name, record["region"], attachment_url)
        key = digest((record["id"] + attachment_url).encode())
        old = store.get("attachment", key)
        att = dict(id=key, announcement_id=record["id"], announcement_url=url,
                   title=title, year=record["year"], region=record["region"], city=city,
                   name=name, url=attachment_url, decision=status, reason=reason,
                   evidence=evidence, status="pending" if status == "selected" else status)
        if old:
            att.update({k: v for k, v in old.items() if k in ("status", "download_id", "error")})
        store.put("attachment", key, att)
        count += 1
    if not count:
        store.event("detail", "NO_ATTACHMENTS", url, "Eligible announcement has no recognized attachment links")
    return record


def discover(store, client, start, max_pages=50, as_of="2026-10-16"):
    """Follow observed HTML pagination only. Stop explicitly on JS-only lists."""
    queue, visited, found = [canonical(start)], set(), set()
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        body = client.fetch(url)
        raw = store.raw(body)
        page = Page(decode_html(body))
        visited.add(url)
        detail_count = 0
        for a in page.links:
            try:
                link = canonical(a["href"], url)
            except ValueError:
                continue
            path = urllib.parse.urlsplit(link).path
            if urllib.parse.urlsplit(link).hostname != urllib.parse.urlsplit(start).hostname:
                continue
            if re.fullmatch(r"/fxqgg/\d+\.jhtml", path):
                detail_count += 1
                candidate = eligibility(a["title"] or a["text"], link, as_of)
                if candidate["status"] in ("eligible", "review"):
                    store.put("candidate", digest(link.encode()), {**candidate, "list_url": url, "list_raw": raw})
                    found.add(link)
            elif path.startswith("/fxqgg/") and (a["rel"] == "next" or re.fullmatch(r"\s*(?:下一页|下页|next|\d+)\s*", a["text"], re.I)):
                if link not in visited and link not in queue:
                    queue.append(link)
        store.put("list_page", digest(url.encode()), dict(url=url, raw_path=raw, detail_links=detail_count, at=now()))
        if not detail_count:
            store.event("discovery", "DYNAMIC_LIST_UNVERIFIED", url,
                        "No HTML detail links. Public dynamic API not verified; import saved pages or HAR. No endpoint guessed.")
            raise AccessStopped("Dynamic list adapter requires an observed public response; coverage unknown")
    if queue:
        store.event("discovery", "PAGE_LIMIT", start, "Pagination unfinished; no completeness claim")
    return found


def pdf_valid(path):
    with Path(path).open("rb") as f:
        if not f.read(8).startswith(b"%PDF-"):
            return False
        f.seek(max(0, Path(path).stat().st_size - 4096))
        return b"%%EOF" in f.read()


def zip_name(info):
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("gb18030")
        except (UnicodeError, LookupError):
            pass
    return name


def safe_member(name):
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    return not (p.is_absolute() or ".." in p.parts or re.match(r"^[a-zA-Z]:", name) or "\x00" in name)


def validate_zip(z, max_files=50000, max_total=12 * 1024**3):
    entries = z.infolist()
    if len(entries) > max_files or sum(i.file_size for i in entries) > max_total:
        raise ValueError("ZIP exceeds entry/uncompressed size limits")
    for info in entries:
        if info.file_size > 1024**3 or info.file_size / max(info.compress_size, 1) > 2000:
            raise ValueError("ZIP exceeds per-member/ratio limits")


def extract_reports(store, attachment, download, max_depth=3):
    hits = 0
    budget = {"bytes": 0, "files": 0}
    def walk(source, chain, depth):
        nonlocal hits
        with zipfile.ZipFile(source) as z:
            validate_zip(z)
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = zip_name(info)
                member = "!".join(chain + [name])
                key = digest((attachment["id"] + member + str(info.header_offset)).encode())
                row = dict(id=key, attachment_id=attachment["id"], member=member,
                           bytes=info.file_size, status="not_report", reason="filename_filter")
                budget["files"] += 1
                budget["bytes"] += info.file_size
                if budget["files"] > 50000 or budget["bytes"] > 12 * 1024**3:
                    raise ValueError("Nested ZIP cumulative limit exceeded")
                if not safe_member(name):
                    row.update(status="review", reason="unsafe_archive_path")
                elif info.flag_bits & 1:
                    row.update(status="review", reason="encrypted_member")
                elif "广州" in name:
                    row.update(status="excluded", reason="guangzhou_excluded")
                elif name.lower().endswith(".zip"):
                    if depth >= max_depth:
                        row.update(status="review", reason="nested_depth_limit")
                    else:
                        # Nested ZIPs spool to disk; never decompress all project folders.
                        try:
                            with tempfile.TemporaryFile() as tmp, z.open(info) as f:
                                copy_limited(f, tmp, 1024**3)
                                tmp.seek(0)
                                walk(tmp, chain + [name], depth + 1)
                            row.update(status="scanned", reason="nested_zip")
                        except (ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as e:
                            row.update(status="review", reason=str(e))
                elif name.lower().endswith(".pdf") and PDF_RE.search(PurePosixPath(name.replace("\\", "/")).name):
                    try:
                        with tempfile.NamedTemporaryFile(dir=store.root, delete=False) as tmp:
                            temp = Path(tmp.name)
                            with z.open(info) as f:
                                copy_limited(f, tmp, 1024**3)
                        try:
                            save_report(temp, member, key)
                            row.update(status="extracted", reason="report_filename")
                        finally:
                            temp.unlink(missing_ok=True)
                    except (ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as e:
                        row.update(status="review", reason=str(e))
                elif name.lower().endswith((".rar", ".7z")):
                    row.update(status="review", reason="unsupported_nested_archive")
                store.put("member", key, row)

    def save_report(temp, member, key):
        nonlocal hits
        if not pdf_valid(temp):
            raise ValueError("Invalid PDF signature or EOF")
        sha = file_hash(temp)
        out = store.root / "reports" / (sha + ".pdf")
        out.parent.mkdir(exist_ok=True)
        if not out.exists():
            with Path(temp).open("rb") as src, out.open("xb") as dst:
                copy_limited(src, dst, 1024**3)
        store.put("report", key, dict(id=key, sha256=sha, path=str(out.relative_to(store.root)),
            original_member=member, attachment_id=attachment["id"], attachment_url=attachment["url"],
            announcement_url=attachment["announcement_url"], title=attachment["title"],
            year=attachment["year"], region=attachment["region"], city=attachment["city"],
            archive_sha256=download["sha256"], evidence=attachment["evidence"],
            report_name=member.split("!")[-1].replace("\\", "/").split("/")[-1],
            project_path=str(PurePosixPath(member.split("!")[-1].replace("\\", "/")).parent),
            bytes=out.stat().st_size, extracted_at=now()))
        hits += 1
    path = store.root / download["path"]
    if download["kind"] == "pdf":
        save_report(path, attachment["name"], digest(attachment["id"].encode()))
    else:
        walk(path, [], 0)
    if not hits:
        store.event("extract", "NO_REPORT_MATCH", attachment["url"],
                    "No filename-matched PDF; implementation plans with other names require review")
    return hits


def copy_limited(src, dst, limit):
    size = 0
    for block in iter(lambda: src.read(256 * 1024), b""):
        size += len(block)
        if size > limit:
            raise ValueError("Expanded member exceeds byte limit")
        dst.write(block)
    return size


def process(store, client, region="广东", limit=None):
    # Guangdong first; Shenzhen is explicitly selected in a separate invocation.
    if limit is None:
        samples = [r for r in store.rows("report") if r["region"] == region and r["evidence"] != "synthetic_test"]
        if not samples:
            raise AccessStopped("Full download requires a successful real report sample for this region first")
    rows = [a for a in store.rows("attachment") if a["region"] == region and a["decision"] == "selected"]
    rows.sort(key=lambda a: (a["year"], a["announcement_url"], a["name"]))
    attempted = 0
    for att in rows:
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        try:
            download = client.download(att["url"])
            att.update(download_id=download["id"], status="downloaded")
            store.put("attachment", att["id"], att)
            hits = extract_reports(store, att, download)
            att.update(status="extracted" if hits else "review", error="" if hits else "no_report_match")
            store.put("attachment", att["id"], att)
        except AccessStopped as e:
            att.update(status="blocked", error=str(e))
            store.put("attachment", att["id"], att)
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, urllib.error.URLError) as e:
            att.update(status="error", error=str(e))
            store.put("attachment", att["id"], att)
            store.event("process", "ATTACHMENT_ERROR", att["url"], str(e))
    return attempted


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_data(store):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = store.root / "exports" / stamp
    out.mkdir(parents=True)
    data = {}
    schemas = {
        "announcement": ["id", "year", "region", "title", "published_date", "status", "reason", "url", "raw_path", "evidence", "batch_key"],
        "attachment": ["id", "year", "region", "city", "name", "decision", "status", "reason", "announcement_url", "url", "download_id", "error", "evidence"],
        "download": ["id", "url", "path", "bytes", "sha256", "kind", "transport", "completed_at"],
        "report": ["id", "year", "region", "city", "title", "project_path", "report_name", "original_member", "path", "sha256", "bytes", "pdf_pages", "text_status", "visual_check", "announcement_url", "attachment_url", "archive_sha256", "evidence"],
        "member": ["id", "attachment_id", "member", "bytes", "status", "reason"],
        "candidate": ["year", "region", "title", "url", "status", "reason", "list_url"],
    }
    for kind, fields in schemas.items():
        rows = store.rows(kind)
        data[kind] = rows
        with (out / (kind + "s.csv")).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({k: csv_safe(row.get(k, "")) for k in fields} for row in rows)
    events = [dict(r) for r in store.db.execute("SELECT * FROM events ORDER BY id")]
    issues = [dict(at=e["at"], stage=e["stage"], code=e["code"], source=e["source"], message=e["message"])
              for e in events if e["code"] not in {"HTTP", "STRUCTURE", "LOCAL_ATTACHMENT_VERIFIED", "REAL_BROWSER_SAMPLE_PASSED"}]
    issues += [dict(at="", stage="attachment", code=a["status"], source=a["url"], message=a.get("error") or a["reason"])
               for a in data["attachment"] if a["status"] in ("error", "blocked", "review")]
    issues += [dict(at="", stage="member", code=m["status"], source=m["member"], message=m["reason"])
               for m in data["member"] if m["status"] == "review"]
    issues += [dict(at="", stage="announcement", code="review", source=a["url"], message=a["reason"])
               for a in data["announcement"] if a["status"] == "review"]
    data["exceptions"] = issues
    with (out / "exceptions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, ["at", "stage", "code", "source", "message"])
        w.writeheader()
        w.writerows({k: csv_safe(v) for k, v in r.items()} for r in issues)
    coverage = []
    for year in range(2022, 2027):
        for city in (*CITIES, "深圳"):
            matched = [a for a in data["attachment"] if a["year"] == year and a["city"] == city and a["decision"] == "selected"]
            reports = [r for r in data["report"] if r["year"] == year and r["city"] == city]
            coverage.append(dict(year=year, city=city, selected_attachments=len(matched),
                completed_attachments=sum(a["status"] == "extracted" for a in matched),
                report_occurrences=len(reports), unique_pdf_hashes=len({r["sha256"] for r in reports}),
                coverage="unknown_not_reconciled"))
    data["coverage"] = coverage
    with (out / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, list(coverage[0]))
        w.writeheader(); w.writerows(coverage)
    batches = []
    for ann in data["announcement"]:
        if ann["status"] != "eligible":
            continue
        linked = [a for a in data["attachment"] if a["announcement_id"] == ann["id"] and a["decision"] == "selected"]
        row = dict(year=ann["year"], region=ann["region"], title=ann["title"], url=ann["url"], evidence=ann["evidence"])
        for city in (*CITIES, "深圳"):
            row[city] = sum(a["city"] == city for a in linked)
        row["inventory_status"] = "observed_links_only_not_complete" if ann["evidence"] == "web_index_observation" else "parsed_attachments_not_reconciled"
        batches.append(row)
    data["batches"] = batches
    with (out / "batches.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["year", "region", "title", "url", "evidence", *CITIES, "深圳", "inventory_status"]
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows({k: csv_safe(v) for k, v in r.items()} for r in batches)
    data["summary"] = dict(version=VERSION, exported_at=now(),
        as_of=(store.get("settings", "scope") or {}).get("as_of", "2026-10-16"),
        announcements=len(data["announcement"]), selected_attachments=sum(a["decision"] == "selected" for a in data["attachment"]),
        reports=len(data["report"]), unique_pdfs=len({r["sha256"] for r in data["report"]}),
        exceptions=len(issues), complete=False, completeness_reason="Dynamic list totals and 2022-2026 batch denominator not verified")
    data["host_states"] = store.rows("host")
    atomic_json(out / "index.json", data)
    print(json.dumps(dict(export=str(out), **data["summary"]), ensure_ascii=False))
    return out


def import_har(store, path, as_of):
    """Read saved public response bodies only; never replay cookies/auth headers."""
    har = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    details = 0
    for entry in har.get("log", {}).get("entries", []):
        url = entry.get("request", {}).get("url", "")
        u = urllib.parse.urlsplit(url)
        if u.hostname not in HOSTS or entry.get("response", {}).get("status") != 200:
            continue
        content = entry["response"].get("content", {})
        text = content.get("text", "")
        body = base64.b64decode(text) if content.get("encoding") == "base64" else text.encode("utf-8")
        if re.fullmatch(r"/fxqgg/\d+\.jhtml", u.path):
            ingest_detail(store, body, canonical(url), as_of, evidence="imported_har_response")
            details += 1
        elif "json" in content.get("mimeType", ""):
            # Retain actual public list data without guessing its schema or replaying it.
            raw = store.raw(body, "json")
            store.event("har", "PUBLIC_JSON_SCHEMA_REVIEW", url, raw)
    if not details:
        store.event("har", "NO_DETAIL_RESPONSES", str(path), "HAR contains no successful /fxqgg/<id>.jhtml response bodies")
    return details


def import_inventory(store, path, as_of):
    """Import explicitly supplied normalized metadata; retain its evidence label."""
    source = Path(path).read_bytes()
    payload = json.loads(source.decode("utf-8-sig"))
    raw = store.raw(source, "inventory.json")
    for item in payload["announcements"]:
        url = canonical(item["url"])
        if urllib.parse.urlsplit(url).hostname not in HOSTS:
            raise ValueError("Inventory source host not permitted")
        evidence = payload.get("evidence", "supplied_inventory")
        # Parse through the same selection rules; this is reconstructed input, not original HTML.
        from html import escape
        html = "<h1>" + escape(item["title"]) + "</h1><p>" + escape(item.get("published_date", "")) + "</p>"
        for att in item.get("attachments", []):
            html += '<a href="' + escape(canonical(att["url"]), quote=True) + '">' + escape(att["name"]) + '</a>'
        ann = ingest_detail(store, html.encode("utf-8"), url, as_of, evidence=evidence)
        ann.update(raw_path=raw, raw_format="normalized_inventory_not_original_html")
        store.put("announcement", ann["id"], ann)
    store.event("import", "PARTIAL_INVENTORY", str(path), payload.get("note", "Inventory has no website completeness guarantee"))


def import_file(store, file, attachment_url, announcement_url=None):
    """Register a completed file saved directly in data/downloads, without copying it."""
    attachment_url = canonical(attachment_url)
    attachments = [a for a in store.rows("attachment") if a["url"] == attachment_url and a["decision"] == "selected"
                   and (announcement_url is None or a["announcement_url"] == canonical(announcement_url))]
    if not attachments:
        raise ValueError("Import an eligible announcement and observed attachment link first")
    source = Path(file).resolve()
    folder = downloads_dir(store)
    if not source.is_relative_to(folder):
        raise ValueError("Save the attachment directly inside " + str(folder) + "; import-file does not copy external files")
    if source.suffix.lower() not in (".zip", ".pdf", ".bin"):
        raise ValueError("Register only a completed .zip/.pdf/.bin file; temporary downloads are not accepted")
    if source.stat().st_size > 4 * 1024**3:
        raise ValueError("Local attachment exceeds 4 GiB limit")
    with source.open("rb") as stream:
        magic = stream.read(8)
    if magic.startswith(b"PK"):
        with zipfile.ZipFile(source) as z:
            validate_zip(z)
            bad = z.testzip()
            if bad:
                raise ValueError("ZIP CRC failed: " + bad)
        kind = "zip"
    elif magic.startswith(b"%PDF-") and pdf_valid(source):
        kind = "pdf"
    else:
        raise ValueError("Local file is not a valid ZIP or PDF")
    key = digest(attachment_url.encode())
    sha = file_hash(source)
    old = store.get("download", key)
    if old and old["sha256"] != sha:
        raise ValueError("Existing attachment has different content; use a new snapshot directory")
    if old:
        existing = registered_download_path(store, old)
        if existing.exists() and existing != source:
            raise ValueError("Attachment is already registered at " + str(existing) + "; use the existing file")
    download = dict(id=key, url=attachment_url, path=str(source.relative_to(store.root)),
                    bytes=source.stat().st_size, sha256=sha, kind=kind, completed_at=now(),
                    transport="imported_browser_download", storage="in_place", original_filename=source.name)
    store.put("download", key, download)
    count = 0
    for attachment in attachments:
        attachment.update(download_id=key, status="downloaded", transport="imported_browser_download")
        store.put("attachment", attachment["id"], attachment)
        hits = extract_reports(store, attachment, download)
        attachment.update(status="extracted" if hits else "review", error="" if hits else "no_report_match")
        store.put("attachment", attachment["id"], attachment)
        count += hits
    store.event("import_file", "LOCAL_ATTACHMENT_VERIFIED", attachment_url,
                json.dumps(dict(bytes=download["bytes"],sha256=sha,report_occurrences=count),ensure_ascii=False))
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--interval", type=float, default=4)
    parser.add_argument("--as-of", default="2026-10-16", help="Snapshot cutoff date; not host-local date")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--url", default="https://www.celma.org.cn/zqxx/index.jhtml")
    disc = sub.add_parser("discover")
    disc.add_argument("--url", default="https://www.celma.org.cn/fxqgg/index.jhtml")
    disc.add_argument("--max-pages", type=int, default=50)
    detail = sub.add_parser("details", help="Read observed detail URLs from a UTF-8 text file")
    detail.add_argument("urls")
    imp = sub.add_parser("import-html")
    imp.add_argument("file"); imp.add_argument("--url", required=True)
    har = sub.add_parser("import-har"); har.add_argument("file")
    inventory = sub.add_parser("import-inventory"); inventory.add_argument("file")
    local = sub.add_parser("import-file", help="Register a completed ZIP/PDF saved in data/downloads; no copying or network")
    local.add_argument("file")
    local.add_argument("--attachment-url", required=True)
    local.add_argument("--announcement-url")
    sub.add_parser("download-dir", help="Print the absolute folder to use as the browser download destination")
    run = sub.add_parser("download")
    run.add_argument("--region", choices=["广东", "深圳"], default="广东")
    run.add_argument("--limit", type=int, default=1, help="Default: one sample archive")
    run.add_argument("--full", action="store_true", help="All selected attachments in the supplied inventory, not verified site completeness")
    sub.add_parser("export")
    args = parser.parse_args(argv)
    date.fromisoformat(args.as_of)
    if args.command == "download-dir":
        folder = (Path(args.data).resolve() / "downloads").resolve()
        if not folder.is_relative_to(Path(args.data).resolve()):
            parser.error("Download directory resolves outside the data directory")
        folder.mkdir(parents=True, exist_ok=True)
        print(str(folder))
        return 0
    store = Store(args.data)
    scope = store.get("settings", "scope")
    if scope and scope.get("as_of") != args.as_of:
        parser.error("Snapshot cutoff differs from this data directory; use a new --data directory")
    store.put("settings", "scope", dict(as_of=args.as_of))
    client = Client(store, args.interval)
    code = 0
    try:
        if args.command == "probe":
            body = client.fetch(args.url)
            raw = store.raw(body)
            page = Page(decode_html(body))
            store.event("probe", "STRUCTURE", args.url, json.dumps(dict(
                raw=raw, title=page.title, links=len(page.links), scripts=page.scripts), ensure_ascii=False))
        elif args.command == "discover":
            discover(store, client, args.url, args.max_pages, args.as_of)
        elif args.command == "details":
            for url in Path(args.urls).read_text(encoding="utf-8-sig").splitlines():
                if url.strip() and not url.startswith("#"):
                    body = client.fetch(canonical(url))
                    ingest_detail(store, body, canonical(url), args.as_of)
        elif args.command == "import-html":
            ingest_detail(store, Path(args.file).read_bytes(), canonical(args.url), args.as_of, evidence="imported_html")
        elif args.command == "import-har":
            import_har(store, args.file, args.as_of)
        elif args.command == "import-inventory":
            import_inventory(store, args.file, args.as_of)
        elif args.command == "import-file":
            if not import_file(store, args.file, args.attachment_url, args.announcement_url):
                code = 2
        elif args.command == "download":
            if args.limit < 1:
                raise ValueError("--limit must be positive")
            process(store, client, args.region, None if args.full else args.limit)
            if any(a["region"] == args.region and a["decision"] == "selected" and a["status"] in ("error", "blocked", "review")
                   for a in store.rows("attachment")):
                code = 2
    except (AccessStopped, ValueError, OSError, urllib.error.URLError) as e:
        store.event(args.command, "RUN_STOPPED", "", str(e))
        print(str(e), file=sys.stderr)
        code = 2
    finally:
        export_data(store)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
