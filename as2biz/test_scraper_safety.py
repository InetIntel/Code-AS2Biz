"""Standalone tests for scraper failure-safety helpers.

Run with: python3 as2biz/test_scraper_safety.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

import scraper


class _Queue:
    def __init__(self):
        self.calls = 0

    def task_done(self):
        self.calls += 1


def test_browser_internal_urls_are_rejected():
    for url in ("", "chrome-error://chromewebdata/", "about:blank", "about:neterror?id=1"):
        assert scraper._is_browser_error_url(url), url
    for url in ("https://example.com/", "http://example.net/about"):
        assert not scraper._is_browser_error_url(url), url


def test_queue_item_acknowledged_once():
    queue = _Queue()
    token = {"done": False}
    assert scraper._mark_queue_item_done_once(queue, token)
    assert not scraper._mark_queue_item_done_once(queue, token)
    assert queue.calls == 1


def test_browser_sandbox_is_enabled_by_default():
    assert "--no-sandbox" not in scraper._chromium_launch_args(False)
    assert "--no-sandbox" in scraper._chromium_launch_args(True)


def test_invalid_tls_is_opt_in():
    saved = scraper.ALLOW_INVALID_TLS
    try:
        scraper.ALLOW_INVALID_TLS = False
        assert "--ignore-certificate-errors" not in scraper._chromium_launch_args(False)
        assert "--ignore-certificate-errors" not in scraper._chromium_launch_args(True)
        scraper.ALLOW_INVALID_TLS = True
        assert "--ignore-certificate-errors" in scraper._chromium_launch_args(False)
    finally:
        scraper.ALLOW_INVALID_TLS = saved


def test_watchdog_report_is_durable():
    class _Archivist:
        cache_dir = ""

    class _Existing:
        pass

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            _Archivist.cache_dir = tmp
            instance = scraper.Scraper(_Archivist(), _Existing())
            row = {
                "seed": "https://example.com/",
                "site": "example.com",
                "status": "failed",
                "reason": scraper.REASON_WATCHDOG_TIMEOUT,
                "details": "synthetic timeout",
                "final_url": None,
                "attempt_no": None,
            }
            await instance._record_report(row)
            events = Path(instance.report_jsonl_path).read_text(encoding="utf-8").splitlines()
            assert len(events) == 1
            event = json.loads(events[0])
            assert event["reason"] == scraper.REASON_WATCHDOG_TIMEOUT
            assert event["run_id"] == instance.run_id

    asyncio.run(run())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} scraper-safety tests passed")
