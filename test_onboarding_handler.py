"""
handle_onboarding_flows の結合テスト（Slack / Sheets は全てモック。外部通信なし）

実行: python test_onboarding_handler.py   （main.py の依存が入った venv で）
"""
import unittest
from unittest import mock

import main
import onboarding as ob

SAKATANI = "【要員追加】\n氏名: 酒谷 尋\nメール: sakatani.jin@cloud-ace.jp\nチーム: インフラ\n期日: 11/2"
URL = "https://x.slack.com/archives/C1/p1761700000123456"


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.replies = []
        p1 = mock.patch.object(main, "send_slack_reply", lambda ch, ts, msg: self.replies.append((ch, ts, msg)))
        p2 = mock.patch.object(main, "WebClient")
        p1.start(); self.client = p2.start().return_value
        self.addCleanup(mock.patch.stopall)

    def call(self, text, event_ts="1761700000.123456", thread_ts=None):
        return main.handle_onboarding_flows("C1", event_ts, thread_ts, text, text.splitlines()[0], "山内", "U9", URL)

    def test_existing_inquiry_falls_through(self):
        self.assertFalse(self.call("【質問】Looker のライセンスについて"))
        self.assertFalse(self.call("山田さんの Asana 権限をお願いします"))
        self.assertEqual(self.replies, [])

    def test_ok_without_preview_falls_through(self):
        self.client.conversations_replies.return_value = {"messages": [{"text": "別の話"}]}
        self.assertFalse(self.call("OK", event_ts="2.0", thread_ts="1.0"))

    def test_preview_does_not_write_ledger(self):
        with mock.patch.object(main, "write_to_spreadsheet") as w:
            self.assertTrue(self.call(SAKATANI))
            w.assert_not_called()
        self.assertIn("11件", self.replies[0][2])

    def test_direct_confirm_without_history_scope(self):
        ws = mock.MagicMock()
        ws.get_all_values.return_value = [[], [], ["1", "", "", "別件", "", "", "", "", "", "", "", "other@x.jp"]]
        with mock.patch.object(main, "default", return_value=(None, None)), \
             mock.patch.object(main.gspread, "authorize") as auth, \
             mock.patch.object(main, "write_to_spreadsheet", return_value=(True, [], list(range(101, 112)))) as w:
            auth.return_value.open_by_key.return_value.worksheet.return_value = ws
            self.assertTrue(self.call(SAKATANI + "\n確定"))
            self.assertEqual(len(w.call_args[0][1]), 11)
            self.client.conversations_replies.assert_not_called()

    def test_direct_confirm_blocks_duplicate(self):
        ws = mock.MagicMock()
        ws.get_all_values.return_value = [[], [], ["101", "", "", "【要員追加】x", "", "", "", "", "", "", "", "sakatani.jin@cloud-ace.jp"]]
        with mock.patch.object(main, "default", return_value=(None, None)), \
             mock.patch.object(main.gspread, "authorize") as auth, \
             mock.patch.object(main, "write_to_spreadsheet") as w:
            auth.return_value.open_by_key.return_value.worksheet.return_value = ws
            self.assertTrue(self.call(SAKATANI + "\n確定"))
            w.assert_not_called()
            self.assertIn("すでに", self.replies[0][2])

    def test_confirm_writes_one_row_per_permission(self):
        p = ob.parse_onboarding_request(SAKATANI)
        preview = ob.build_preview_message(p, ob.expand_ledger_entries(p, ob.load_packs()), ob.load_packs())
        self.client.conversations_replies.return_value = {"messages": [{"text": SAKATANI}, {"text": preview}]}
        with mock.patch.object(main, "write_to_spreadsheet", return_value=(True, list(range(3, 13)), list(range(101, 111)))) as w:
            self.assertTrue(self.call("OK 除く: HCP Terraform", event_ts="2.0", thread_ts="1.0"))
            entries = w.call_args[0][1]
            self.assertEqual(len(entries), 10)
            self.assertNotIn("HCP Terraform", [e["tags"][2] for e in entries])

    def test_confirm_twice_is_blocked(self):
        p = ob.parse_onboarding_request(SAKATANI)
        preview = ob.build_preview_message(p, ob.expand_ledger_entries(p, ob.load_packs()), ob.load_packs())
        self.client.conversations_replies.return_value = {
            "messages": [{"text": preview}, {"text": main.REGISTERED_MARKER + " 登録しました"}]}
        with mock.patch.object(main, "write_to_spreadsheet") as w:
            self.assertTrue(self.call("OK", event_ts="3.0", thread_ts="1.0"))
            w.assert_not_called()

    def test_missing_scope_is_reported(self):
        self.client.conversations_replies.side_effect = Exception("missing_scope")
        self.assertTrue(self.call("OK", event_ts="2.0", thread_ts="1.0"))
        self.assertIn("history 権限", self.replies[0][2])


if __name__ == "__main__":
    unittest.main()
