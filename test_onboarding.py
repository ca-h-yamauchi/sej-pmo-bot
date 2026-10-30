"""
要員追加（権限パック）ロジックの単体テスト。外部通信なし。

実行: python test_onboarding.py
"""
import unittest

import onboarding as ob

PACKS = ob.load_packs()

SAKATANI = """【要員追加】
氏名: 酒谷 尋
メール: sakatani.jin@cloud-ace.jp
チーム: インフラ
期日: 11/2"""


class TestRegression(unittest.TestCase):
    """既存の問合せ・課題の文面が、要員追加として誤検知されないこと（デグレ防止）"""

    EXISTING_FIRST_LINES = [
        "【質問】Looker のライセンス数を教えてください",
        "【確認】佐藤さんの Jira 権限の件",
        "【課題】Terraform の apply が失敗する",
        "山田さんに Asana の権限をお願いします",
        "アカウント管理: tanaka@cloud-ace.jp を Slack に追加",
        "新規メンバーの追加をお願いします",
        "要員のアサインについて相談です",
    ]

    def test_existing_not_onboarding(self):
        for line in self.EXISTING_FIRST_LINES:
            self.assertFalse(ob.is_onboarding_request(line), line)
            self.assertFalse(ob.is_completion_request(line), line)

    def test_long_text_is_not_confirmation(self):
        self.assertFalse(ob.is_confirmation("OK" + "あ" * 100))
        self.assertFalse(ob.is_confirmation("オーダ番号を教えてください"))
        self.assertTrue(ob.is_confirmation("OK"))
        self.assertTrue(ob.is_confirmation("OK 除く: HCP Terraform"))


class TestParse(unittest.TestCase):
    def test_sakatani(self):
        p = ob.parse_onboarding_request(SAKATANI)
        self.assertEqual(p["name"], "酒谷 尋")
        self.assertEqual(p["email"], "sakatani.jin@cloud-ace.jp")
        self.assertEqual(p["team"], "infra")
        self.assertEqual(p["due"], "11/2")
        self.assertEqual(p["missing"], [])

    def test_missing(self):
        p = ob.parse_onboarding_request("【要員追加】\n氏名: 山田 花子")
        self.assertIn("メールアドレス", p["missing"])
        self.assertTrue(any("チーム" in m for m in p["missing"]))

    def test_exclusions(self):
        self.assertEqual(ob.parse_exclusions("OK 除く: HCP Terraform, 次世代"), ["HCP Terraform", "次世代"])
        self.assertEqual(ob.parse_exclusions("OK"), [])


class TestExpand(unittest.TestCase):
    def person(self, team):
        return {"name": "酒谷 尋", "email": "sakatani.jin@cloud-ace.jp", "team": team, "due": "11/2"}

    def test_infra_one_row_per_permission(self):
        entries = ob.expand_ledger_entries(self.person("infra"), PACKS)
        tags = [e["tags"][2] for e in entries]
        self.assertEqual(len(entries), 11)
        self.assertEqual(len(set(tags)), 11)  # 1権限種別 = 1行
        for t in ("7central.jp GWS", "Googleグループ(共通7件)", "Googleグループ(ca-infra)", "HCP Terraform", "次世代店舗"):
            self.assertIn(t, tags)

    def test_looker_has_no_infra_or_nextgen(self):
        tags = [e["tags"][2] for e in ob.expand_ledger_entries(self.person("looker"), PACKS)]
        self.assertNotIn("HCP Terraform", tags)
        self.assertNotIn("Googleグループ(ca-infra)", tags)
        self.assertNotIn("次世代店舗", tags)

    def test_exclusion(self):
        entries = ob.expand_ledger_entries(self.person("infra"), PACKS, ["HCP Terraform"])
        self.assertNotIn("HCP Terraform", [e["tags"][2] for e in entries])
        self.assertEqual(len(entries), 10)

    def test_entry_shape_matches_ledger_writer(self):
        e = ob.expand_ledger_entries(self.person("infra"), PACKS)[0]
        for key in ("target_name", "target_email", "tags", "details", "due_date", "order_number", "order_name"):
            self.assertIn(key, e)
        self.assertEqual(e["tags"][0], "アカウント管理")
        self.assertEqual(len(e["tags"]), 5)

    def test_groups_listed_in_details(self):
        entries = ob.expand_ledger_entries(self.person("infra"), PACKS)
        common = next(e for e in entries if e["tags"][2] == "Googleグループ(共通7件)")
        self.assertIn("ca-dev@7central.jp", common["details"])
        self.assertIn("ca-mlops-crm-pi-dev@7central.jp", common["details"])


class TestPreviewRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        p = ob.parse_onboarding_request(SAKATANI)
        msg = ob.build_preview_message(p, ob.expand_ledger_entries(p, PACKS), PACKS)
        meta = ob.find_preview_in_thread([{"text": "【要員追加】..."}, {"text": msg}, {"text": "OK"}])
        self.assertEqual(meta["email"], "sakatani.jin@cloud-ace.jp")
        self.assertEqual(meta["team"], "infra")
        self.assertEqual(meta["due"], "11/2")

    def test_no_preview(self):
        self.assertIsNone(ob.find_preview_in_thread([{"text": "OK"}]))


class TestCompletion(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(ob.parse_completion_numbers("【完了連絡】問合せNo 101, 102-104"), [101, 102, 103, 104])
        self.assertEqual(ob.parse_completion_numbers("【完了連絡】101 101"), [101])

    def test_slack_url(self):
        u = ob.parse_slack_url("https://x.slack.com/archives/C0123ABC/p1761700000123456")
        self.assertEqual(u, {"channel": "C0123ABC", "thread_ts": "1761700000.123456"})
        self.assertIsNone(ob.parse_slack_url(""))

    def _row(self, tag):
        return ["101", "", "", "", "", "アカウント管理", "アカウント新規申請登録", tag, "インフラ", "", "酒谷 尋", "x@y", "", "", "", "1.未着手"]

    def test_message_with_link_and_secret(self):
        msg = ob.build_completion_message(self._row("7central.jp GWS"), PACKS, "<@U1>")
        self.assertIn("<@U1>", msg)
        self.assertIn("確認済み", msg)
        self.assertIn("DM", msg)
        self.assertNotIn("パスワードは:", msg)

    def test_no_self_check_for_groups(self):
        msg = ob.build_completion_message(self._row("Googleグループ(共通7件)"), PACKS, "<@U1>")
        self.assertIn("個別の確認は不要", msg)

    def test_match_user_by_name(self):
        members = [
            {"id": "U1", "real_name": "酒谷 尋", "profile": {"display_name": ""}},
            {"id": "U2", "real_name": "佐藤 洋一", "profile": {}},
            {"id": "U3", "real_name": "Old", "deleted": True, "profile": {}},
        ]
        self.assertEqual(ob.match_user_by_name(members, "酒谷 尋", "sakatani.jin@cloud-ace.jp"), "U1")
        self.assertIsNone(ob.match_user_by_name(members, "山田 花子", "yamada.hanako@x.jp"))
        members.append({"id": "U4", "real_name": "酒谷 尋(別人)", "profile": {}})
        self.assertIsNone(ob.match_user_by_name(members, "酒谷 尋"))  # 複数候補は特定しない
        romaji = [{"id": "U5", "real_name": "Jin Sakatani", "profile": {}}]
        self.assertEqual(ob.match_user_by_name(romaji, "", "sakatani.jin@cloud-ace.jp"), "U5")

    def test_unknown_tag_fallback(self):
        self.assertIn("確認", ob.build_completion_message(self._row("未知"), PACKS, "@x"))


if __name__ == "__main__":
    unittest.main()
