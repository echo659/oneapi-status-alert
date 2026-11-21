import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.state.write_text(json.dumps({"agentspace": False, "bard": True}))
        p = patch.object(monitor, "STATE_FILE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def run_monitor(self, result, argv=None):
        with patch.object(monitor, "fetch_status", return_value=result) as fetch, \
             patch.object(monitor, "send_email") as send, \
             patch.object(sys, "argv", argv or ["monitor.py"]):
            monitor.main()
            return fetch, send

    def test_empty_port(self):
        for env in ({}, {"SMTP_PORT": ""}, {"SMTP_PORT": " "}, {"SMTP_PORT": "465"}):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(monitor.smtp_port(), 465)

    def test_test_email_does_not_touch_state_or_website(self):
        before = self.state.read_bytes()
        fetch, send = self.run_monitor({}, ["monitor.py", "--test-email"])
        fetch.assert_not_called()
        send.assert_called_once_with([], test=True)
        self.assertEqual(before, self.state.read_bytes())

    def test_recovery_and_dedup(self):
        result = {"agentspace": {"status": "健康", "capacity": 2},
                  "bard": {"status": "健康", "capacity": 3}}
        _, send = self.run_monitor(result)
        send.assert_called_once_with([("agentspace", "健康", "2")])
        _, send = self.run_monitor(result)
        send.assert_not_called()

    def test_zero_capacity_then_infinity(self):
        result = {"agentspace": {"status": "健康", "capacity": 0},
                  "bard": {"status": "休整中", "capacity": 4}}
        _, send = self.run_monitor(result)
        send.assert_not_called()
        result["agentspace"]["capacity"] = "∞"
        _, send = self.run_monitor(result)
        send.assert_called_once_with([("agentspace", "健康", "∞")])

    def test_failed_read_preserves_state(self):
        result = {"agentspace": {"error": "unavailable"}, "bard": {"error": "unavailable"}}
        for _ in range(2):
            _, send = self.run_monitor(result)
            send.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.run_monitor(result)
        state = json.loads(self.state.read_text())
        self.assertTrue(state["bard"])
        self.assertFalse(state["agentspace"])
        self.assertEqual(state["_failure_counts"]["bard"], 3)
        self.run_monitor({"agentspace": {"status": "休整中", "capacity": 0},
                          "bard": {"status": "健康", "capacity": 3}})
        self.assertEqual(json.loads(self.state.read_text())["_failure_counts"]["bard"], 0)

    def test_send_failure_keeps_unnotified(self):
        with patch.object(monitor, "fetch_status", return_value={
            "agentspace": {"status":"健康", "capacity":1},
            "bard": {"status":"休整中", "capacity":0}}), \
            patch.object(monitor, "send_email", side_effect=RuntimeError("SMTP")), \
            patch.object(sys, "argv", ["monitor.py"]):
            with self.assertRaises(RuntimeError):
                monitor.main()
        state = json.loads(self.state.read_text())
        self.assertFalse(state["agentspace"])
        self.assertFalse(state["bard"])


class BrowserTests(unittest.TestCase):
    def test_top_badges_and_capacity(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                page.set_content("""<style>
                .card{width:280px;height:150px;border:1px solid; padding:10px;margin:8px;display:inline-block;vertical-align:top}
                .head{display:flex;justify-content:space-between}
                .columns{display:grid;grid-template-columns:1fr 1fr;text-align:center;margin-top:20px}
                </style>
                <div class="card"><div class="head"><span>agentsp...</span><span>休整中</span></div>
                <div class="columns"><span>容量</span><span>队列</span><span>0</span><span>9</span></div>
                <div>正常 · 1 worker</div><div>健康</div></div>
                <div class="card"><div class="head"><span>bard</span><span>健康</span></div>
                <div class="columns"><span>容量</span><span>队列</span><span>2</span><span>99</span></div></div>
                """)
                data = page.evaluate(monitor.EXTRACT_JS, list(monitor.CHANNELS))
                self.assertEqual(data["agentspace"], {"status": "休整中", "capacity": 0})
                self.assertEqual(data["bard"], {"status": "健康", "capacity": 2})
                page.locator(".card").nth(1).locator(".head span").nth(1).evaluate("(e) => e.remove()")
                data = page.evaluate(monitor.EXTRACT_JS, list(monitor.CHANNELS))
                self.assertIn("error", data["bard"])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
