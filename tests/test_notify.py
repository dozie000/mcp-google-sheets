import os
import unittest
from unittest.mock import patch, MagicMock

from mcp_google_sheets import notify

ENV = {"DECIDE_TOKEN": "secret", "NTFY_TOPIC": "t-123"}


class NotifyTests(unittest.TestCase):
    def test_bad_key_rejected_and_nothing_sent(self):
        with patch.dict(os.environ, ENV), patch("urllib.request.urlopen") as op:
            r = notify.send_push_impl("nope", "T", "hi")
            self.assertEqual(r, {"ok": False, "error": "bad key"})
            op.assert_not_called()

    def test_unconfigured(self):
        with patch.dict(os.environ, {"DECIDE_TOKEN": "", "NTFY_TOPIC": ""}):
            self.assertFalse(notify.send_push_impl("x", "T", "hi")["ok"])

    def test_click_must_be_https(self):
        with patch.dict(os.environ, ENV), patch("urllib.request.urlopen") as op:
            r = notify.send_push_impl("secret", "T", "hi", "http://x.example")
            self.assertFalse(r["ok"])
            op.assert_not_called()

    def test_sends_headers_and_body(self):
        resp = MagicMock(status=200)
        resp.__enter__.return_value = resp
        with patch.dict(os.environ, ENV), patch("urllib.request.urlopen", return_value=resp) as op:
            r = notify.send_push_impl("secret", "CCORE — 15 of 188", "body",
                                      "https://example.run.app/d/x", 4)
            self.assertTrue(r["ok"])
            req = op.call_args[0][0]
            self.assertEqual(req.full_url, "https://ntfy.sh/t-123")
            self.assertEqual(req.data, b"body")
            h = {k.lower(): v for k, v in req.header_items()}
            self.assertEqual(h["title"], "CCORE - 15 of 188")
            self.assertEqual(h["click"], "https://example.run.app/d/x")
            self.assertEqual(h["priority"], "4")

    def test_register_requires_env(self):
        from mcp.server.fastmcp import FastMCP
        import asyncio
        with patch.dict(os.environ, {"DECIDE_TOKEN": "", "NTFY_TOPIC": ""}):
            m = FastMCP("a"); notify.register(m)
            self.assertEqual([t.name for t in asyncio.run(m.list_tools())], [])
        with patch.dict(os.environ, ENV):
            m = FastMCP("b"); notify.register(m)
            self.assertEqual([t.name for t in asyncio.run(m.list_tools())], ["send_push"])


if __name__ == "__main__":
    unittest.main()
