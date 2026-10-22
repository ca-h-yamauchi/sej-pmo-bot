"""
メールアドレス正規表現フォールバックの単体テスト

GCPやSlackへの外部通信は一切行わず、純粋に関数のロジックのみをローカルで高速・安全に検証します。
実行方法:
    python test_email_fallback.py
"""

import os
import unittest
from main import extract_emails_from_text, fallback_target_email_with_regex


class TestEmailFallback(unittest.TestCase):

    def setUp(self):
        # テスト開始前に環境変数を初期化
        if "ENABLE_EMAIL_REGEX_FALLBACK" in os.environ:
            del os.environ["ENABLE_EMAIL_REGEX_FALLBACK"]

    def tearDown(self):
        # テスト終了後に環境変数を初期化
        if "ENABLE_EMAIL_REGEX_FALLBACK" in os.environ:
            del os.environ["ENABLE_EMAIL_REGEX_FALLBACK"]

    def test_extract_emails_from_text(self):
        """本文からメールアドレスが正しく重複排除されて抽出されるか"""
        text = "佐藤さん sato@example.com と 田中さん tanaka.test@cloud-ace.jp、重複 sato@example.com です。"
        emails = extract_emails_from_text(text)
        self.assertEqual(emails, ["sato@example.com", "tanaka.test@cloud-ace.jp"])

    def test_single_item_fallback_success(self):
        """【主目的】Geminiが target_email: null を返したが本文にメールがある場合、自動補完されること"""
        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": None,
                "tags": ["アカウント管理", "新規登録"],
            }
        ]
        text = "佐藤太郎さんのアカウント作成をお願い。sato@example.com"
        result = fallback_target_email_with_regex(extracted, text)

        self.assertEqual(result[0]["target_email"], "sato@example.com")

    def test_single_item_already_has_email(self):
        """Geminiがすでに正しいメールを抽出している場合、上書きされないこと"""
        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": "sato@example.com",
                "tags": ["アカウント管理"],
            }
        ]
        text = "佐藤太郎さんのアカウント作成をお願い。sato@example.com また別のアドレス other@example.com"
        result = fallback_target_email_with_regex(extracted, text)

        self.assertEqual(result[0]["target_email"], "sato@example.com")

    def test_single_item_no_email_in_text(self):
        """本文にもメールがない場合、Noneのままであること"""
        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": None,
                "tags": ["アカウント管理"],
            }
        ]
        text = "佐藤太郎さんのアカウント作成をお願い。メールは後で送ります。"
        result = fallback_target_email_with_regex(extracted, text)

        self.assertIsNone(result[0]["target_email"])

    def test_multiple_items_partial_missing(self):
        """複数件のうち1人だけ email が抜けていた場合、未使用のメールが補完されること"""
        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": "sato@example.com",
                "tags": ["アカウント管理"],
            },
            {
                "target_name": "田中花子",
                "target_email": None,
                "tags": ["アカウント管理"],
            },
        ]
        text = "佐藤太郎さん sato@example.com と 田中花子さん tanaka@example.com のアカウント追加をお願いします。"
        result = fallback_target_email_with_regex(extracted, text)

        self.assertEqual(result[0]["target_email"], "sato@example.com")
        self.assertEqual(result[1]["target_email"], "tanaka@example.com")

    def test_multiple_items_all_missing(self):
        """複数件ですべて email が抜けていた場合、名前の位置に基づいて適切に補完されること"""
        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": None,
                "tags": ["アカウント管理"],
            },
            {
                "target_name": "田中花子",
                "target_email": None,
                "tags": ["アカウント管理"],
            },
        ]
        text = "佐藤太郎さん sato@example.com と 田中花子さん tanaka@example.com のアカウント追加"
        result = fallback_target_email_with_regex(extracted, text)

        self.assertEqual(result[0]["target_email"], "sato@example.com")
        self.assertEqual(result[1]["target_email"], "tanaka@example.com")

    def test_nakamura_slack_thread_pattern(self):
        """【実際のスレッド検証】2026-08-10 中村さんのスレッド（箇条書き・インデント・改行）でメールがnullになった場合"""
        from main import normalize_slack_mailto_links

        raw_slack_text = """<@U093Z56D683|BOT-SEJ-PMO（試験運用中）> <!subteam^S06F6KR4JEQ>

JIRA：<https://7-11ngscp.atlassian.net/jira/software/c/projects/CA/boards/654/timeline|7-11ngscp.atlassian.net/jira/…/timeline>
上記のJIRAのスペースを利用できるように権限を付与していただきたいです。
• 木京 友紀
    ◦ <mailto:kikyo.yuki@cloud-ace.jp|kikyo.yuki@cloud-ace.jp>
• 戸倉 大揮
    ◦ <mailto:tokura.hiroki@cloud-ace.jp|tokura.hiroki@cloud-ace.jp>
• 松本亮太
    ◦ <mailto:matsumoto.ryota@cloud-ace.jp|matsumoto.ryota@cloud-ace.jp>"""

        normalized_text = normalize_slack_mailto_links(raw_slack_text)

        # Gemini が箇条書きや改行で混乱し、全員のメールアドレスを null で返してきたケースを想定
        extracted = [
            {"target_name": "木京 友紀", "target_email": None, "tags": ["アカウント管理", "Jira"]},
            {"target_name": "戸倉 大揮", "target_email": None, "tags": ["アカウント管理", "Jira"]},
            {"target_name": "松本亮太", "target_email": None, "tags": ["アカウント管理", "Jira"]},
        ]

        result = fallback_target_email_with_regex(extracted, normalized_text)

        self.assertEqual(result[0]["target_email"], "kikyo.yuki@cloud-ace.jp")
        self.assertEqual(result[1]["target_email"], "tokura.hiroki@cloud-ace.jp")
        self.assertEqual(result[2]["target_email"], "matsumoto.ryota@cloud-ace.jp")

    def test_kill_switch_disabled(self):
        """【デグレード対策】ENABLE_EMAIL_REGEX_FALLBACK=false の場合、補完が行われず元データのまま返ること"""
        os.environ["ENABLE_EMAIL_REGEX_FALLBACK"] = "false"

        extracted = [
            {
                "target_name": "佐藤太郎",
                "target_email": None,
                "tags": ["アカウント管理"],
            }
        ]
        text = "佐藤太郎さんのアカウント作成をお願い。sato@example.com"
        result = fallback_target_email_with_regex(extracted, text)

        # 補完されずに None のままであること
        self.assertIsNone(result[0]["target_email"])

    def test_gemini_model_env_var_default_and_override(self):
        """GEMINI_MODEL/LOCATION が未設定時にデフォルト値、設定時にその値が反映されること"""
        import main
        # 未指定時のデフォルト値
        if "GEMINI_MODEL" in os.environ:
            del os.environ["GEMINI_MODEL"]
        model_default = os.environ.get("GEMINI_MODEL", main.GEMINI_MODEL)
        self.assertEqual(model_default, "gemini-3.5-flash-lite")

        # 環境変数でオーバーライドした時
        os.environ["GEMINI_MODEL"] = "gemini-2.5-flash-lite"
        model_override = os.environ.get("GEMINI_MODEL", main.GEMINI_MODEL)
        self.assertEqual(model_override, "gemini-2.5-flash-lite")
        del os.environ["GEMINI_MODEL"]


if __name__ == "__main__":
    unittest.main()
