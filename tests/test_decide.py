import os
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_google_sheets import decide

MOVE = [
    ["Approve? (Y/N)", "Handler (Me/Coach)", "Current Path", "Category", "Subfolder",
     "Proposed New Path", "Size", "Modified", "Notes"],
    ["", "", "Downloads/a.jpg", "Images", "Photos", "Downloads/Images/Photos/a.jpg", "1 KB", "2024-01-01", ""],
    ["", "", "Downloads/lease.pdf", "Documents", "Legal", "Downloads/Documents/Legal/lease.pdf", "2 KB", "2024-01-02", ""],
    ["", "", "Downloads/dup.pdf", "Documents", "Other", "Downloads/Documents/Other/dup.pdf", "3 KB", "2024-01-03", "Possible duplicate of: x.pdf"],
    ["Y", "Coach", "Downloads/done.pdf", "Documents", "Other", "x", "1 KB", "2024-01-04", ""],
    ["Y", "", "Downloads/half.pdf", "Documents", "Other", "x", "1 KB", "2024-01-05", ""],
]
DELETE = [
    ["banner"],
    ["Approve? (Y/N)", "Handler (Me/Coach)", "Current Path", "Category", "Size", "Modified", "Notes"],
    ["", "", "Downloads/~$x.docx", "JUNK - Delete Candidate", "162 B", "2024-02-13"],
]
QUEUE = [
    ["id", "agent", "title", "detail", "Proposed Handler", "Needs You",
     "Approve? (Y/N)", "Handler (Me/Coach)", "Link", "Created UTC"],
    ["t-1", "Capture Sweep", "Call the dentist", "from c-0004", "Me", "", "", "", "", ""],
    ["t-2", "Weekly Routine", "Archive Q3 notes", "", "Coach", "", "", "", "https://example.com/x", ""],
    ["t-3", "Weekly Routine", "Pick the 7 for next week", "", "", "Y", "", "", "javascript:alert(1)", ""],
    ["t-4", "Capture Sweep", "Already done", "", "Coach", "", "Y", "Coach", "", ""],
]


class Fake:
    def __init__(self):
        self.tabs = {"Organize Proposal": [list(r) for r in MOVE],
                     "Delete Candidates": [list(r) for r in DELETE],
                     "Task Queue": [list(r) for r in QUEUE]}
        self.writes = []

    def read(self, tab):
        return self.tabs[tab]

    def write(self, updates):
        self.writes.extend(updates)


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.patches = [
            patch.object(decide, "_read_tab", self.fake.read),
            patch.object(decide, "_write_cells", self.fake.write),
            patch.dict(os.environ, {"DECIDE_TOKEN": "secret", "DECIDE_WRITE": "1"}),
        ]
        for p in self.patches:
            p.start()
        app = Starlette()
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("t")
        decide.register(mcp)
        self.client = TestClient(mcp.streamable_http_app())

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_batch_skips_decided_and_flags_manual(self):
        items = decide.get_batch()["items"]
        names = [i["name"] for i in items]
        self.assertEqual(sorted(names), sorted([
            "a.jpg", "lease.pdf", "dup.pdf", "half.pdf", "~$x.docx",
            "Call the dentist", "Archive Q3 notes", "Pick the 7 for next week"]))
        by = {i["name"]: i for i in items}
        self.assertIsNotNone(by["a.jpg"]["proposal"])
        self.assertIsNone(by["lease.pdf"]["proposal"])
        self.assertIsNone(by["dup.pdf"]["proposal"])
        self.assertEqual(by["~$x.docx"]["row"], 3)
        self.assertEqual(by["~$x.docx"]["kind"], "delete")

    def test_batch_includes_sheet_link(self):
        url = decide.get_batch()["sheet_url"]
        self.assertTrue(url.startswith("https://docs.google.com/spreadsheets/d/"))
        self.assertTrue(url.endswith("/edit"))

    def test_page_links_the_sheet_safely(self):
        self.assertIn("setSub(", decide.PAGE_HTML if hasattr(decide, "PAGE_HTML") else open(decide.__file__).read())

    def test_task_queue_items_and_proposals(self):
        by = {i["name"]: i for i in decide.get_batch(50)["items"]}
        t = by["Call the dentist"]
        self.assertEqual((t["agent"], t["kind"], t["path"]), ("Capture Sweep", "task", "t-1"))
        self.assertEqual(t["proposal"], {"approve": "Y", "handler": "Me"})
        self.assertIsNone(by["Pick the 7 for next week"]["proposal"])
        self.assertEqual(by["Pick the 7 for next week"]["manual_reason"], "Needs your judgment")
        self.assertEqual(by["Archive Q3 notes"]["link"], "https://example.com/x")
        self.assertEqual(by["Pick the 7 for next week"]["link"], "")  # non-https dropped
        self.assertNotIn("Already done", by)

    def test_agent_counts_and_cap_round_robin(self):
        with patch.dict(os.environ, {"DECIDE_AGENT_CAP": "2"}):
            d = decide.get_batch(5)
        self.assertEqual(d["agent_counts"]["Folder CleanUp"], 5)
        self.assertEqual(d["agent_counts"]["Weekly Routine"], 2)
        agents = [i["agent"] for i in d["items"]]
        self.assertLessEqual(agents.count("Folder CleanUp"), 2)
        self.assertEqual(len(agents), 5)
        self.assertIn("Capture Sweep", agents)
        self.assertIn("Weekly Routine", agents)

    def test_cap_relaxes_when_one_agent_has_all_the_work(self):
        self.fake.tabs["Task Queue"] = [QUEUE[0]]
        d = decide.get_batch(4)
        self.assertEqual(len(d["items"]), 4)  # cap 5 >= 4 anyway
        with patch.dict(os.environ, {"DECIDE_AGENT_CAP": "1"}):
            self.assertEqual(len(decide.get_batch(4)["items"]), 4)

    def test_missing_queue_tab_does_not_break_cleanup(self):
        del self.fake.tabs["Task Queue"]
        d = decide.get_batch(50)
        self.assertEqual(d["pending_total"], 5)

    def test_task_answer_writes_queue_columns(self):
        res = decide.apply_answers([{"tab": "Task Queue", "row": 2, "path": "t-1",
                                     "approve": "Y", "handler": "Me"}])
        self.assertEqual(res[0]["status"], "ok")
        self.assertEqual(self.fake.writes, [{"tab": "Task Queue",
                                             "range": "'Task Queue'!G2:H2",
                                             "values": [["Y", "Me"]]}])
        stale = decide.apply_answers([{"tab": "Task Queue", "row": 2, "path": "t-9",
                                       "approve": "Y", "handler": "Me"}])
        self.assertEqual(stale[0]["status"], "stale")

    def test_apply_writes_approve_and_handler_columns(self):
        res = decide.apply_answers([{"tab": "Organize Proposal", "row": 2,
                                     "path": "Downloads/a.jpg", "approve": "Y", "handler": "Coach"}])
        self.assertEqual(res[0]["status"], "ok")
        self.assertEqual(self.fake.writes, [{"tab": "Organize Proposal",
                                             "range": "'Organize Proposal'!A2:B2",
                                             "values": [["Y", "Coach"]]}])

    def test_stale_path_is_not_written(self):
        res = decide.apply_answers([{"tab": "Organize Proposal", "row": 2,
                                     "path": "Downloads/other.jpg", "approve": "Y", "handler": "Me"}])
        self.assertEqual(res[0]["status"], "stale")
        self.assertEqual(self.fake.writes, [])

    def test_existing_decision_not_overwritten(self):
        res = decide.apply_answers([{"tab": "Organize Proposal", "row": 5,
                                     "path": "Downloads/done.pdf", "approve": "N", "handler": "n/a"}])
        self.assertEqual(res[0]["status"], "already_decided")
        self.assertEqual(self.fake.writes, [])

    def test_invalid_values_rejected(self):
        for bad in ({"approve": "maybe", "handler": "Me"}, {"approve": "Y", "handler": "Bob"}):
            res = decide.apply_answers([{"tab": "Organize Proposal", "row": 2,
                                         "path": "Downloads/a.jpg", **bad}])
            self.assertEqual(res[0]["status"], "invalid")
        res = decide.apply_answers([{"tab": "Organize Proposal", "row": 1,
                                     "path": "x", "approve": "Y", "handler": "Me"}])
        self.assertEqual(res[0]["status"], "invalid")  # header row is never writable
        self.assertEqual(self.fake.writes, [])

    def test_delete_tab_uses_its_own_header_row(self):
        res = decide.apply_answers([{"tab": "Delete Candidates", "row": 3,
                                     "path": "Downloads/~$x.docx", "approve": "Y", "handler": "Coach"}])
        self.assertEqual(res[0]["status"], "ok")
        self.assertEqual(self.fake.writes[0]["range"], "'Delete Candidates'!A3:B3")

    def test_http_token_and_modes(self):
        self.assertEqual(self.client.get("/d/wrong").status_code, 404)
        self.assertEqual(self.client.get("/d/wrong/api/batch").status_code, 404)
        self.assertEqual(self.client.post("/d/wrong/api/answer", json={"answers": []}).status_code, 404)
        page = self.client.get("/d/secret")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/d/secret/api/batch").json()["pending_total"], 8)
        ok = self.client.post("/d/secret/api/answer", json={"answers": [
            {"tab": "Organize Proposal", "row": 2, "path": "Downloads/a.jpg", "approve": "Y", "handler": "Coach"}]})
        self.assertEqual(ok.json()["results"][0]["status"], "ok")
        self.assertEqual(self.client.post("/d/secret/api/answer", json={"nope": 1}).status_code, 400)

    def test_read_only_blocks_writes(self):
        with patch.dict(os.environ, {"DECIDE_WRITE": "0"}):
            r = self.client.post("/d/secret/api/answer", json={"answers": []})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(self.client.get("/d/secret/api/batch").json()["write_enabled"])

    def test_routes_absent_without_token(self):
        from mcp.server.fastmcp import FastMCP
        with patch.dict(os.environ, {"DECIDE_TOKEN": ""}):
            mcp = FastMCP("t2")
            decide.register(mcp)
            self.assertEqual(TestClient(mcp.streamable_http_app()).get("/d/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
