"""Continue collecting observed Guangdong announcement URLs, skipping recorded gaps.

Works alongside collector.py v0.2.1. No third-party packages, migration or copying
of previously collected files. The built-in URL list is partial, not a five-year
inventory or a random sample. Shenzhen requires an explicit separate invocation.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
from pathlib import Path

import collector as c


# Public detail URLs observed in the website/browser investigation on 2026-10-16.
# No numeric ID enumeration and no guessed dynamic API requests.
GUANGDONG_URLS = (
    "https://www.celma.org.cn/fxqgg/70584.jhtml",  # 2026, already collected batch
    "https://www.celma.org.cn/fxqgg/57698.jhtml",  # 2022
    "https://www.celma.org.cn/fxqgg/57701.jhtml",  # 2022
    "https://www.celma.org.cn/fxqgg/57697.jhtml",  # 2022
    "https://www.celma.org.cn/fxqgg/58568.jhtml",  # 2023
    "https://www.celma.org.cn/fxqgg/61184.jhtml",  # 2024
    "https://www.celma.org.cn/fxqgg/64934.jhtml",  # 2024, reposted in 2025
    "https://www.celma.org.cn/fxqgg/64113.jhtml",  # 2025
    "https://www.celma.org.cn/fxqgg/64263.jhtml",  # 2025
    "https://www.celma.org.cn/fxqgg/64750.jhtml",  # 2025
    "https://www.celma.org.cn/fxqgg/65481.jhtml",  # 2025
)
CLOSED = {"extracted", "error", "review"}


def detail_urls(lines):
    result = []
    for line in lines:
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        url = c.canonical(line)
        parsed = c.urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.celma.org.cn" or not re.fullmatch(r"/fxqgg/\d+\.jhtml", parsed.path):
            raise ValueError("Only observed CELMA /fxqgg/<id>.jhtml detail URLs are accepted: " + url)
        if url not in result:
            result.append(url)
    return result


class AnnouncementView:
    """Give the existing processor only the pending rows for one announcement."""
    def __init__(self, store, attachments):
        self.store = store
        self.ids = {a["id"] for a in attachments}

    def __getattr__(self, key):
        return getattr(self.store, key)

    def rows(self, kind):
        rows = self.store.rows(kind)
        return [a for a in rows if a["id"] in self.ids] if kind == "attachment" else rows


class ProgressClient:
    def __init__(self, client, attachments, stats):
        self.client, self.stats = client, stats
        self.names = {a["url"]: a["name"] for a in attachments}

    def download(self, url):
        self.stats["attachments_attempted"] += 1
        print("[ZIP " + str(self.stats["attachments_attempted"]) + "] " + self.names.get(url, url), flush=True)
        try:
            return self.client.download(url)
        except http.client.HTTPException as e:
            # Preserve the partial file while allowing the core's normal error path.
            raise OSError("HTTP transfer failed: " + str(e)) from e


def collect_known(store, client, urls=GUANGDONG_URLS, region="广东", limit=10,
                  max_announcements=0, as_of="2026-10-16"):
    if limit < 0 or max_announcements < 0:
        raise ValueError("Limits must be nonnegative; 0 means no run cap")
    urls = detail_urls(urls)
    stats = dict(scope="observed_urls_only_not_complete", source_urls=len(urls),
                 announcements_processed=0, attachments_attempted=0,
                 extracted=0, failed=0, review=0, skipped_completed=0,
                 skipped_failed=0, skipped_detail_errors=0, detail_errors=0,
                 policy="skip_recorded_failures_no_retry")
    print("Continue collection: " + str(len(urls)) + " observed announcement URLs; this is not the complete 2022-2026 inventory.", flush=True)
    print("Completed/failed attachments are skipped. Existing PDFs and error records are kept.", flush=True)
    try:
        for url in urls:
            if limit and stats["attachments_attempted"] >= limit:
                break
            if max_announcements and stats["announcements_processed"] >= max_announcements:
                break
            key = c.digest(url.encode())
            ann = store.get("announcement", key)
            prior = store.get("continuation_detail", key)
            if ann is None:
                if prior and prior.get("status") == "error":
                    stats["skipped_detail_errors"] += 1
                    print("[skip previous page error] " + url, flush=True)
                    continue
                print("[page] " + url, flush=True)
                try:
                    body = client.fetch(url)
                    ann = c.ingest_detail(store, body, url, as_of, evidence="live_http")
                    store.put("continuation_detail", key, dict(url=url, status="parsed", at=c.now()))
                except c.AccessStopped:
                    raise  # robots/WAF/429 stops are never treated as skippable gaps.
                except (OSError, ValueError, UnicodeError, http.client.HTTPException) as e:
                    stats["detail_errors"] += 1
                    store.put("continuation_detail", key, dict(url=url, status="error", error=str(e), at=c.now()))
                    store.event("continue_detail", "DETAIL_ERROR", url, str(e))
                    print("[page failed; skipped] " + str(e), flush=True)
                    continue
            if ann.get("status") != "eligible" or ann.get("region") != region:
                continue
            attachments = [a for a in store.rows("attachment")
                           if a["announcement_id"] == ann["id"] and a["decision"] == "selected"]
            for a in attachments:
                if a["status"] == "blocked":
                    raise c.AccessStopped("Previously blocked attachment; access restriction remains: " + a["url"])
                if a["status"] == "extracted":
                    stats["skipped_completed"] += 1
                elif a["status"] in ("error", "review"):
                    stats["skipped_failed"] += 1
            pending = [a for a in attachments if a["status"] not in CLOSED]
            if not pending:
                print("[skip closed announcement] " + ann["title"], flush=True)
                continue
            print("[collect] " + ann["title"] + " | pending ZIPs: " + str(len(pending)), flush=True)
            remaining = limit - stats["attachments_attempted"] if limit else None
            view = AnnouncementView(store, pending)
            before = stats["attachments_attempted"]
            try:
                c.process(view, ProgressClient(client, pending, stats), region, remaining)
            finally:
                # Include completed work even if an access stop interrupts the batch.
                attempted = stats["attachments_attempted"] - before
                ordered = sorted(pending, key=lambda a: (a["year"], a["announcement_url"], a["name"]))
                for a in ordered[:attempted]:
                    updated = store.get("attachment", a["id"])
                    status = updated["status"]
                    if status == "extracted":
                        stats["extracted"] += 1
                    elif status == "error":
                        stats["failed"] += 1
                        print("[failed; kept as gap] " + a["name"] + ": " + updated.get("error", ""), flush=True)
                    elif status == "review":
                        stats["review"] += 1
                stats["announcements_processed"] += 1
        return stats
    finally:
        store.put("collection_run", c.now(), stats)
        print(json.dumps({"continuation": stats}, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--interval", type=float, default=4)
    parser.add_argument("--as-of", default="2026-10-16")
    parser.add_argument("--region", choices=["广东", "深圳"], default="广东")
    parser.add_argument("--urls-file", help="Optional UTF-8 file of observed detail URLs; required for Shenzhen")
    parser.add_argument("--limit", type=int, default=10, help="New ZIP attempts this run; 0 for all pending in the observed URL list")
    parser.add_argument("--max-announcements", type=int, default=0, help="Announcements with pending work this run; 0 for no cap")
    args = parser.parse_args(argv)
    c.date.fromisoformat(args.as_of)
    if args.region == "深圳" and not args.urls_file:
        parser.error("Provide a separate --urls-file for Shenzhen")
    if args.limit < 0 or args.max_announcements < 0:
        parser.error("Limits cannot be negative")
    urls = Path(args.urls_file).read_text(encoding="utf-8-sig").splitlines() if args.urls_file else GUANGDONG_URLS
    urls = detail_urls(urls)
    store = c.Store(args.data)
    scope = store.get("settings", "scope")
    if scope and scope.get("as_of") != args.as_of:
        store.db.close()
        parser.error("Use the existing snapshot cutoff for this data directory")
    store.put("settings", "scope", dict(as_of=args.as_of))
    code = 0
    try:
        collect_known(store, c.Client(store, args.interval), urls, args.region,
                      args.limit, args.max_announcements, args.as_of)
    except (c.AccessStopped, OSError, ValueError) as e:
        store.event("continue_collect", "RUN_STOPPED", "", str(e))
        print(str(e), file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("Interrupted; completed work and gaps are saved.", file=sys.stderr)
        code = 130
    finally:
        try:
            c.export_data(store)
        finally:
            store.db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
