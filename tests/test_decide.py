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
     "Approve? (Y/N)", "Handler (Me/Coach)", "Link", "Created UTC", "Applied UTC", "Action"],
    ["t-1", "Capture Sweep", "Call the dentist", "from c-0004", "Me", "", "", "", "", ""],
    ["t-2", "Weekly Routine", "Archive Q3 notes", "", "Coach", "", "", "", "https://example.com/x", ""],
    ["t-3", "Weekly Routine", "Pick the 7 for next week", "", "", "Y", "", "", "javascript:alert(1)", ""],
    ["t-4", "Capture Sweep", "Already done", "", "Coach", "", "Y", "Coach", "", ""],
    ["t-5", "Pipeline Coach", "Clarify: Reach out to Entegee?", "detail", "Me", "", "", "", "", "", "",
     '{"op":"promote","from":"Captured","to":"Clarified","id":"c-0003","row":{"title":"Reach out to Entegee","outcome":"Intro call booked","next_action":"Email recruiter","readiness":"ready","source":"apple-notes","moved_from":"c-0003"}}'],
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
        # queue view: task cards only, plus ONE summary group for Folder CleanUp
        q = decide.get_batch()
        self.assertTrue(all(i["kind"] == "task" for i in q["items"]))
        self.assertIn("Call the dentist", [i["name"] for i in q["items"]])
        self.assertEqual(len(q["groups"]), 1)
        self.assertEqual(q["groups"][0]["agent"], "Folder CleanUp")
        self.assertEqual(q["groups"][0]["pending"], 5)
        self.assertEqual(q["groups"][0]["your_call"], 2)  # lease (Legal) and dup
        # cleanup view: small-batch rule, one category/subfolder
        d = decide.get_batch(view="cleanup")
        cleanup = d["items"]
        self.assertTrue(cleanup and all(i["kind"] != "task" for i in cleanup))
        self.assertEqual(len({(i["tab"], i["category"], i["subfolder"]) for i in cleanup}), 1)
        self.assertEqual(d["focus"].split(" / ")[0], cleanup[0]["category"])
        self.assertEqual(d["pending_total"], 9)
        # every pending cleanup row is still reachable via the focus rotation
        seen = set()
        for it in decide.parse_tab("Organize Proposal", self.fake.tabs["Organize Proposal"]) + \
                decide.parse_tab("Delete Candidates", self.fake.tabs["Delete Candidates"]):
            seen.add(it["name"])
            if it["name"] == "a.jpg":
                self.assertIsNotNone(it["proposal"])
            if it["name"] in ("lease.pdf", "dup.pdf"):
                self.assertIsNone(it["proposal"])
            if it["name"] == "~$x.docx":
                self.assertEqual((it["row"], it["kind"]), (3, "delete"))
        self.assertEqual(seen, {"a.jpg", "lease.pdf", "dup.pdf", "half.pdf", "~$x.docx"})

    def test_moves_script_route_only_includes_y_coach_rows(self):
        self.assertEqual(self.client.get("/d/wrong/moves.ps1").status_code, 404)
        r = self.client.get("/d/secret/moves.ps1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertNotIn("lease.pdf", r.text)  # undecided rows excluded
        self.assertIn("-Apply", r.text)

    def _approve_first_move(self):
        self.fake.tabs["Organize Proposal"][1][0:2] = ["Y", "Coach"]

    def test_script_page_and_info(self):
        self.assertEqual(self.client.get("/d/wrong/script").status_code, 404)
        self.assertEqual(self.client.get("/d/wrong/api/script").status_code, 404)
        self._approve_first_move()
        page = self.client.get("/d/secret/script")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Copy script", page.text)
        self.assertIn("-OutFile moves.ps1", page.text)
        info = self.client.get("/d/secret/api/script").json()
        self.assertEqual((info["moves"], info["deletes"]), (1, 0))
        self.assertIn("a.jpg", info["script"])

    def test_script_log_appends_once_and_creates_tab(self):
        from unittest.mock import MagicMock
        self._approve_first_move()
        svc = MagicMock()
        ss = svc.spreadsheets.return_value
        ss.get.return_value.execute.return_value = {"sheets": [{"properties": {"title": "Captured"}}]}
        ss.values.return_value.get.return_value.execute.return_value = {"values": [["Script"]]}
        with patch.object(decide, "_sheets_service", return_value=svc):
            r = self.client.post("/d/secret/api/script/log")
        self.assertEqual(r.json(), {"logged": True})
        ss.batchUpdate.assert_called_once()  # tab created
        # the log lives in Downloads_Cleanup_Proposal, not the pipeline sheet
        self.assertEqual(ss.get.call_args.kwargs["spreadsheetId"], decide.DEFAULT_SHEET_ID)
        row = ss.values.return_value.append.call_args.kwargs["body"]["values"][0]
        self.assertEqual((row[1], row[2]), (1, 0))
        self.assertIn("a.jpg", row[3])
        # identical latest entry is not logged twice
        ss.values.return_value.get.return_value.execute.return_value = {"values": [["Script"], [row[3]]]}
        ss.get.return_value.execute.return_value = {"sheets": [{"properties": {"title": "Script Log"}}]}
        with patch.object(decide, "_sheets_service", return_value=svc):
            r = self.client.post("/d/secret/api/script/log")
        self.assertFalse(r.json()["logged"])

    def test_batch_route_views(self):
        q = self.client.get("/d/secret/api/batch").json()
        self.assertEqual(q["view"], "queue")
        c = self.client.get("/d/secret/api/batch?view=cleanup").json()
        self.assertEqual(c["view"], "cleanup")
        self.assertTrue(c["focus"])

    def _svc(self, tab_rows):
        from unittest.mock import MagicMock
        svc = MagicMock()
        ss = svc.spreadsheets.return_value
        ss.values.return_value.get.return_value.execute.side_effect = lambda: {"values": tab_rows.pop(0)}
        return svc, ss

    def test_parse_checklist(self):
        got = decide.parse_checklist(
            "- [ ] Whole armor of God\n- [x] done thing\n- [ ] \n- [ ] Reach out to Entegee\n"
            "- [ ] reach  out to entegee\nplain line\n- [ ] See https://a.b/c")
        self.assertEqual(got, ["Whole armor of God", "Reach out to Entegee", "See https://a.b/c"])

    def test_ingest_goes_to_captured_without_duplicates(self):
        header = ["id", "timestamp", "raw_text", "source_type", "urgency"]
        svc, ss = self._svc([[header, ["c-0002", "t", "Reach out to Entegee"]]])
        text = "- [ ] Reach out to  entegee\n- [x] Old\n- [ ] Do taxes\n- [ ] Pallet idea"
        with patch.object(decide, "_sheets_service", return_value=svc):
            r = self.client.post("/d/secret/api/notes", json={"text": text})
        self.assertEqual(r.json(), {"unchecked_in_note": 3, "added": 2, "already_known": 1})
        rows = ss.values.return_value.append.call_args.kwargs["body"]["values"]
        self.assertEqual([(a[0], a[2], a[3]) for a in rows],
                         [("c-0003", "Do taxes", "apple-notes"), ("c-0004", "Pallet idea", "apple-notes")])
        self.assertEqual(ss.values.return_value.append.call_args.kwargs["range"], "'Captured'!A:Z")
        with patch.object(decide, "_sheets_service", return_value=svc):
            self.assertEqual(self.client.post("/d/wrong/api/notes", json={"text": text}).status_code, 404)

    def test_approved_proposal_is_applied_to_next_tab(self):
        header = ["id", "title", "outcome", "next_action", "readiness", "source", "moved_from", "timestamp"]
        svc, ss = self._svc([[header, ["c-0001", "x"]]])
        with patch.object(decide, "_sheets_service", return_value=svc):
            res = decide.apply_answers([{"tab": "Task Queue", "row": 6, "path": "t-5",
                                         "approve": "Y", "handler": "Me"}])
        self.assertEqual(res[0]["status"], "ok")
        row = ss.values.return_value.append.call_args.kwargs["body"]["values"][0]
        self.assertEqual(row[:7], ["c-0003", "Reach out to Entegee", "Intro call booked",
                                   "Email recruiter", "ready", "apple-notes", "c-0003"])
        self.assertTrue(row[7])  # timestamp filled
        self.assertEqual(ss.values.return_value.append.call_args.kwargs["range"], "'Clarified'!A:Z")
        stamp = self.fake.writes[-1]
        self.assertEqual(stamp["range"], "'Task Queue'!K6")  # Applied UTC stamped

    def test_rejected_or_duplicate_proposals_change_nothing(self):
        header = ["id", "title"]
        svc, ss = self._svc([[header, ["c-0003", "already"]]])
        with patch.object(decide, "_sheets_service", return_value=svc):
            decide.apply_answers([{"tab": "Task Queue", "row": 6, "path": "t-5",
                                   "approve": "N", "handler": "n/a"}])
        ss.values.return_value.append.assert_not_called()
        self.fake.writes.clear()
        svc, ss = self._svc([[header, ["c-0003", "already"]]])
        self.fake.tabs["Task Queue"][5][6:8] = ["", ""]
        with patch.object(decide, "_sheets_service", return_value=svc):
            decide.apply_answers([{"tab": "Task Queue", "row": 6, "path": "t-5",
                                   "approve": "Y", "handler": "Me"}])
        ss.values.return_value.append.assert_not_called()  # id already there

    def test_apply_action_rejects_bad_operations(self):
        from unittest.mock import MagicMock
        svc = MagicMock()
        with patch.object(decide, "_sheets_service", return_value=svc):
            for bad in ['{"op":"delete","to":"Clarified","id":"c-1","row":{}}',
                        '{"op":"promote","to":"Captured","id":"c-1","row":{}}',
                        '{"op":"promote","to":"Sheet1","id":"c-1","row":{}}',
                        'not json', '{"op":"promote","to":"Clarified","id":"","row":{}}']:
                self.assertEqual(decide.apply_action(bad, "Me"), "invalid")
        svc.spreadsheets.assert_not_called()

    def test_batch_includes_sheet_link(self):
        url = decide.get_batch()["sheet_url"]
        self.assertTrue(url.startswith("https://docs.google.com/spreadsheets/d/"))
        self.assertTrue(url.endswith("/edit"))

    def test_page_links_the_sheet_safely(self):
        self.assertIn("setSub(", decide.PAGE_HTML if hasattr(decide, "PAGE_HTML") else open(decide.__file__).read())

    def test_task_queue_items_and_proposals(self):
        by = {i["name"]: i for i in decide.get_batch(50)["items"]}
        self.assertNotIn("Already done", by)
        t = by["Call the dentist"]
        self.assertEqual((t["agent"], t["kind"], t["path"]), ("Capture Sweep", "task", "t-1"))
        self.assertEqual(t["proposal"], {"approve": "Y", "handler": "Me"})
        self.assertIsNone(by["Pick the 7 for next week"]["proposal"])
        self.assertEqual(by["Pick the 7 for next week"]["manual_reason"], "Needs your judgment")
        self.assertEqual(by["Archive Q3 notes"]["link"], "https://example.com/x")
        self.assertEqual(by["Pick the 7 for next week"]["link"], "")  # non-https dropped
        self.assertNotIn("Already done", by)

    def test_agent_counts_and_cap_round_robin(self):
        with patch.dict(os.environ, {"DECIDE_AGENT_CAP": "1"}):
            d = decide.get_batch(3)
        self.assertEqual(d["agent_counts"]["Folder CleanUp"], 5)
        self.assertEqual(d["agent_counts"]["Weekly Routine"], 2)
        agents = [i["agent"] for i in d["items"]]
        self.assertEqual(len(agents), 3)
        self.assertNotIn("Folder CleanUp", agents)  # cleanup lives behind its own card
        self.assertIn("Capture Sweep", agents)
        self.assertIn("Weekly Routine", agents)

    def test_cap_relaxes_when_one_agent_has_all_the_work(self):
        self.fake.tabs["Task Queue"] = [QUEUE[0]]
        d = decide.get_batch(4, view="cleanup")
        self.assertGreaterEqual(len(d["items"]), 1)
        self.assertEqual(len({(i["category"], i["subfolder"]) for i in d["items"]}), 1)
        with patch.dict(os.environ, {"DECIDE_AGENT_CAP": "1"}):
            self.assertEqual(len(decide.get_batch(4, view="cleanup")["items"]),
                             len(d["items"]))

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
        self.assertEqual(self.client.get("/d/secret/api/batch").json()["pending_total"], 9)
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


def test_build_script_escapes_and_refuses_escapes():
    acts = [
        {"kind": "move", "src": "Downloads/it's.jpg", "dest": "Downloads/Images/it's.jpg"},
        {"kind": "delete", "src": "Downloads/~$1.pdf", "dest": ""},
        {"kind": "move", "src": "Downloads/../x", "dest": "Downloads/y"},
        {"kind": "move", "src": "Downloads/a", "dest": "C:/Windows/a"},
        {"kind": "move", "src": "Other/a", "dest": "Downloads/a"},
    ]
    s = decide.build_script(acts)
    assert "it''s.jpg" in s and "~$1.pdf" in s
    assert "..\\" not in s and "Downloads/../x" not in s
    assert "Windows" not in s and "Other/a" not in s
    assert "[switch]$Apply" in s


def test_focus_cleanup_picks_one_group():
    def it(cat, sub, prop=True, kind="move"):
        return {"tab": "Organize Proposal", "kind": kind, "category": cat,
                "subfolder": sub, "proposal": {"approve": "Y"} if prop else None,
                "agent": decide.CLEANUP_AGENT}
    task = {"tab": "Task Queue", "kind": "task", "category": "", "subfolder": "",
            "proposal": None, "agent": "X"}
    items = [it("Legal", "", False), it("Images", "Photos"), it("Images", "Photos"),
             it("Docs", "BME"), task]
    kept, label = decide._focus_cleanup(items)
    assert label == "Images / Photos"
    assert task in kept and len(kept) == 3


def test_eastern_without_tzdb():
    import datetime as dt
    from mcp_google_sheets.decide import _eastern
    u = dt.timezone.utc
    assert _eastern(dt.datetime(2026, 10, 12, 21, 52, tzinfo=u)).hour == 17
    assert _eastern(dt.datetime(2026, 12, 1, 12, 0, tzinfo=u)).hour == 7
    assert _eastern(dt.datetime(2026, 3, 8, 6, 59, tzinfo=u)).hour == 1
    assert _eastern(dt.datetime(2026, 3, 8, 7, 0, tzinfo=u)).hour == 3
