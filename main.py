"""
Slack アカウント申請自動化システム
Cloud Functions (第2世代) 用エントリーポイント
"""
import os
import json
import logging
import secrets
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from calendar import monthrange

import re
import functions_framework
from flask import Request
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from google.cloud import aiplatform
import gspread
from google.auth import default

import onboarding

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数の取得
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SPREADSHEET_KEY = os.environ.get("SPREADSHEET_KEY")
PROJECT_ID = os.environ.get("PROJECT_ID")
# 本番の Vertex AI ロケーション（3.x 世代 Flash-Lite は global エンドポイントを使用）
LOCATION = os.environ.get("LOCATION", "global")
# 使用する Gemini モデル（デフォルト: 最新・高コスパの gemini-3.5-flash-lite）
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
CLOUD_RUN_REVISION = os.environ.get("K_REVISION", "local")

# 署名検証用のインスタンス
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET) if SLACK_SIGNING_SECRET else None


def generate_error_id() -> str:
    """Cloud Logging と Slack 返信を突き合わせる短い ID を払う。"""
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(3)}"


def sanitize_error_text(text: str, limit: int = 200) -> str:
    """トークン類を伏せ、Slack 向けに短くする。"""
    if not text:
        return ""
    redacted = re.sub(r"xox[baprs]-[A-Za-z0-9-]+", "[REDACTED]", text)
    redacted = re.sub(r"(?i)(token|secret|authorization)[=:]\s*\S+", r"\1=[REDACTED]", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    if len(redacted) > limit:
        return redacted[:limit] + "…"
    return redacted


def classify_error_stage(tb_text: str) -> str:
    """traceback から処理段階を推定する。"""
    lowered = tb_text.lower()
    if "extract_info_with_gemini" in tb_text or "vertexai" in lowered or "aiplatform" in lowered:
        return "Gemini 抽出"
    if "write_to_spreadsheet" in tb_text or "gspread" in lowered:
        return "Sheets 書き込み"
    if "send_slack_reply" in tb_text:
        return "Slack 返信"
    return "その他"


def build_slack_error_message(error_id: str, stage: str, exc: BaseException) -> str:
    """社内解析用の短いエラー文面を作る（機密は出さない）。"""
    detail = sanitize_error_text(str(exc))
    return (
        "エラーが発生しました。しばらく時間をおいて再度お試しください。\n"
        "解析用情報:\n"
        f"- 発生箇所: {stage}\n"
        f"- 例外: {type(exc).__name__}: {detail}\n"
        f"- error_id: {error_id}\n"
        f"- リビジョン: {CLOUD_RUN_REVISION}\n"
        "Cloud Logging で error_id を検索してください。"
    )


def normalize_due_date(due_date_str: Optional[str]) -> Optional[str]:
    """
    期日文字列を正規化する（相対的な日付表現を実際の日付に変換）
    
    Args:
        due_date_str: 期日文字列（例：「１月中」「来月まで」「今月末」など）
        
    Returns:
        正規化された日付文字列（YYYY-MM-DD形式）、変換できない場合は元の文字列を返す
    """
    if not due_date_str or due_date_str.lower() in ["null", "none", ""]:
        return None
    
    # 既にYYYY-MM-DD形式の場合はそのまま返す
    if re.match(r'^\d{4}-\d{2}-\d{2}$', due_date_str):
        return due_date_str
    
    # 日本時間で現在時刻を取得
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    current_year = now.year
    current_month = now.month
    
    # 「○月中」「○月末」のパターン（数字は全角・半角両対応）
    month_pattern = r'([０-９0-9]+)月(中|末|まで)'
    match = re.search(month_pattern, due_date_str)
    if match:
        month_str = match.group(1)
        # 全角数字を半角に変換
        month_str = month_str.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        try:
            month = int(month_str)
            if 1 <= month <= 12:
                if match.group(2) == "末":
                    # その月の最終日
                    _, last_day = monthrange(current_year, month)
                    return f"{current_year}-{month:02d}-{last_day:02d}"
                elif match.group(2) in ["中", "まで"]:
                    # その月の最終日（「中」は月末までという意味として解釈）
                    _, last_day = monthrange(current_year, month)
                    return f"{current_year}-{month:02d}-{last_day:02d}"
        except ValueError:
            pass
    
    # 「今月末」「今月中」のパターン
    if re.search(r'今月(末|中|まで)', due_date_str):
        _, last_day = monthrange(current_year, current_month)
        return f"{current_year}-{current_month:02d}-{last_day:02d}"
    
    # 「来月」「来月末」「来月中」のパターン
    if re.search(r'来月(末|中|まで)?', due_date_str):
        next_month = current_month + 1
        next_year = current_year
        if next_month > 12:
            next_month = 1
            next_year += 1
        _, last_day = monthrange(next_year, next_month)
        return f"{next_year}-{next_month:02d}-{last_day:02d}"
    
    # 「来週」「来週末」のパターン
    if re.search(r'来週(末|まで)?', due_date_str):
        days_until_next_week = 7 - now.weekday()  # 次の月曜日までの日数
        next_week_date = now + timedelta(days=days_until_next_week + 6)  # 次の日曜日
        return next_week_date.strftime("%Y-%m-%d")
    
    # 「今週末」「今週まで」のパターン
    if re.search(r'今週(末|まで)', due_date_str):
        days_until_sunday = 6 - now.weekday()  # 今週の日曜日までの日数
        this_weekend = now + timedelta(days=days_until_sunday)
        return this_weekend.strftime("%Y-%m-%d")
    
    # 「○日後」「○日以内」のパターン
    days_pattern = r'([０-９0-9]+)日(後|以内|まで)'
    match = re.search(days_pattern, due_date_str)
    if match:
        days_str = match.group(1)
        days_str = days_str.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        try:
            days = int(days_str)
            target_date = now + timedelta(days=days)
            return target_date.strftime("%Y-%m-%d")
        except ValueError:
            pass
    
    # 変換できない場合は元の文字列を返す（Geminiが既に正しい形式で返している可能性がある）
    logger.warning(f"期日の正規化に失敗: {due_date_str}")
    return due_date_str


def normalize_slack_mailto_links(text: str) -> str:
    """
    Slack API が返すメールの自動リンク表記を平文に戻す。
    例: <mailto:user@example.com|user@example.com> → user@example.com
    """
    return re.sub(
        r"<mailto:([^|>]+)(?:\|[^>]*)?>",
        lambda m: m.group(1).strip(),
        text,
        flags=re.IGNORECASE,
    )


def extract_emails_from_text(text: str) -> List[str]:
    """
    テキストからメールアドレスを重複を除いて出現順に抽出する。
    """
    pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    found = re.findall(pattern, text)
    seen = set()
    unique_emails = []
    for email in found:
        cleaned = email.rstrip(".")
        if cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            unique_emails.append(cleaned)
    return unique_emails


def fallback_target_email_with_regex(
    extracted_data_list: List[Dict[str, Any]],
    text: str,
) -> List[Dict[str, Any]]:
    """
    Gemini で target_email が抽出されなかった場合（null または空）、
    本文から正規表現でメールアドレスを拾って補完する。

    安全機構（キルスイッチ）:
    環境変数 ENABLE_EMAIL_REGEX_FALLBACK が 'false', '0', 'off', 'no' の場合は
    一切の補完を行わず、元の抽出結果をそのまま返す（デグレード時の即時ロールバック用）。
    """
    kill_switch = os.environ.get("ENABLE_EMAIL_REGEX_FALLBACK", "true").lower()
    if kill_switch in ("false", "0", "off", "no"):
        logger.info("ENABLE_EMAIL_REGEX_FALLBACK が無効化されているため、メール正規表現フォールバックをスキップします。")
        return extracted_data_list

    if not extracted_data_list:
        return extracted_data_list

    all_emails = extract_emails_from_text(text)
    if not all_emails:
        return extracted_data_list

    # 既に抽出済みのメールアドレスを把握
    already_assigned = set()
    for item in extracted_data_list:
        e = item.get("target_email")
        if e and isinstance(e, str) and e.strip():
            already_assigned.add(e.strip().lower())

    # 未使用のメールアドレス候補
    available_emails = [e for e in all_emails if e.lower() not in already_assigned]
    if not available_emails:
        return extracted_data_list

    # 単一依頼の場合（最頻パターン）
    if len(extracted_data_list) == 1:
        item = extracted_data_list[0]
        if not item.get("target_email"):
            picked = available_emails[0]
            item["target_email"] = picked
            logger.info(f"【正規表現フォールバック】単一依頼で target_email を補完しました: {picked}")
        return extracted_data_list

    # 複数依頼の場合
    for item in extracted_data_list:
        if not item.get("target_email") and available_emails:
            target_name = item.get("target_name")
            picked = None
            # 対象者の名前が本文にあれば、その名前の直後に出現するメールを優先探索
            if target_name and target_name in text:
                name_idx = text.find(target_name)
                for candidate in available_emails:
                    cand_idx = text.find(candidate)
                    if cand_idx >= name_idx:
                        picked = candidate
                        break
            if not picked:
                picked = available_emails[0]

            item["target_email"] = picked
            available_emails.remove(picked)
            logger.info(f"【正規表現フォールバック】複数依頼で target_email を補完しました: target_name={target_name}, email={picked}")

    return extracted_data_list


def extract_info_with_gemini(text: str, inquirer_name: str, is_issue: bool = False) -> List[Dict[str, Any]]:
    """
    Vertex AI (Gemini 2.5 Flash Lite) を使用してテキストから情報を抽出する
    
    Args:
        text: 抽出対象のテキスト
        inquirer_name: 問合せ者のユーザー名（表示名、実名、またはUser ID）
        is_issue: 課題管理モードの場合True（課題管理用プロンプトを使用）
        
    Returns:
        抽出された情報の辞書のリスト（複数依頼に対応）
    """
    try:
        # モデル名: 環境変数 GEMINI_MODEL で指定可能（デフォルト: gemini-3.5-flash-lite）
        model_name = os.environ.get("GEMINI_MODEL", GEMINI_MODEL)

        # ロケーションの決定（3.x 世代モデルは global エンドポイントが必須なため、非対応リージョン指定時は自動フォールバック）
        current_location = os.environ.get("LOCATION", LOCATION)
        if ("3." in model_name or "3-" in model_name) and current_location != "global":
            logger.info(f"モデル {model_name} は global エンドポイントが必須のため、LOCATION={current_location} から global へ自動調整します。")
            current_location = "global"

        logger.info(f"Vertex AI初期化: PROJECT_ID={PROJECT_ID}, LOCATION={current_location}")
        aiplatform.init(project=PROJECT_ID, location=current_location)
        
        from vertexai.generative_models import GenerativeModel
        logger.info(f"使用するモデル: {model_name} (リージョン: {current_location})")
        model = GenerativeModel(model_name)
        
        if is_issue:
            prompt = f"""
以下のテキストから、課題・問題事項に関する情報を抽出してください。

【コンテキスト情報】
この報告は「{inquirer_name}」からのものです。

【抽出・分類ルール】
1. 1つのメッセージに複数の課題がある場合は、それぞれを別のエントリとして分割してください。
2. 各エントリについて以下の情報を抽出してください：

- issue_title: 課題タイトル（メッセージ内容から核心を20文字以内で端的に要約）
- issue_summary: 課題の概要（状況・影響・背景などを含む詳細なサマリー）
- tags: 対象システム名やバグ/仕様/運用などのカテゴリを表すタグの配列（例：["Slack", "バグ", "アカウント管理"]）
- priority: 優先度（メッセージから推測できる場合は High / Mid / Low のいずれか。不明な場合はnull）
- due_date: 目標解決日（明示的な日付の場合はYYYY-MM-DD形式で返す。相対的な表現（例：「１月中」「来月末」「今週末」など）の場合は、そのままの表現で返すこと。不明な場合はnull）

テキスト: {text}

必ず以下のJSON配列形式で返答してください（複数の課題がある場合は配列に複数の要素を含める）:
[
    {{
        "issue_title": "課題タイトル（20文字以内）",
        "issue_summary": "課題の概要（詳細なサマリー）",
        "tags": ["タグ1", "タグ2"],
        "priority": "High/Mid/Lowまたはnull",
        "due_date": "目標解決日（明示的な日付の場合はYYYY-MM-DD形式、相対的な表現の場合はそのままの表現、不明な場合はnull）"
    }}
]
"""
        else:
            prompt = f"""
以下のテキストから、アカウント申請や作業依頼に関する情報を抽出してください。

【コンテキスト情報】
この問い合わせは「{inquirer_name}」からのものです。
もし「私」や「自分」のアカウント等の言及があれば、対象者を「{inquirer_name}」として扱ってください。
ただし、対象者の氏名やメールアドレスは、メッセージ本文から抽出してください。

【抽出・分類ルール】
1. 1つのメッセージに複数の依頼がある場合は、それぞれを別のエントリとして分割してください。
2. 各エントリについて以下の情報を抽出してください：

【対象者情報】
- target_name: 対象者の氏名（「私」の場合はメッセージ本文から抽出。「{inquirer_name}」を指す可能性が高い）
- target_email: 対象者のメールアドレス（不明な場合はnull）
  * 本文は通常の「local@domain」形式で渡されますが、もし角括弧や記号が残っていても中身のメールアドレスだけを抽出してください。
  * 「氏名」「メール」「Email」「アドレス」などのラベルの直後や、同じ行・隣接する行に書かれたメール形式の文字列は、文脈から対象者のものであれば必ず target_email に設定してください（見落とさないこと）。
  * アカウント管理・新規申請・権限などの依頼で、本文に1件でも有効なメールアドレスがあれば、それを対象者のメールとして返してください。依頼者（問い合わせ者）用と明示されていない限り、依頼の対象となる人物のメールとして扱ってください。

【タグ情報】
この問い合わせの属性を表すタグを最大5つまで設定してください。
- タグの例：「アカウント管理」「アカウント新規申請登録」「スラック」「課題」「作業依頼」など、問い合わせの種類や内容を表すタグ
- 1つの問い合わせに対して複数のタグを設定できます
  * 例1：（社内ツールである）RMS登録の作業依頼 → ["作業依頼", "RMS登録", null, null, null]
  * 例2：Slackに関する改善したい事項の問い合わせ → ["課題", "Slack", null, null, null]
  * 例3：アカウント管理でSlack関連、権限の追加に関する問い合わせ → ["アカウント管理", "権限追加", "Slack", null, null]
- 重要：問い合わせ内に所属を表す情報（例：「営業のAさん」「SREチームのBさん」「コンサルティング部のCさん」など）が明示的に含まれている場合は、その所属情報もタグに含めてください。
  * 例：「営業のAさんのAsanaアカウント追加」→ ["アカウント管理", "新規登録", "Asana", "営業", null]
  * 例：「SREチームのBさんのSlackアカウント作成」→ ["アカウント管理", "新規登録", "スラック", "SREチーム", null]
- tags: タグの配列（最大5つ、不足する場合はnullで埋める。必ず5つの要素を持つ配列として返すこと）

【その他】
- details: 概要・詳細（不明な場合はnull）
- due_date: 対応期日（作業して欲しい期日が明示されている場合のみ記載。明示的な日付（例：「2024-01-31」）の場合はYYYY-MM-DD形式で返す。相対的な表現（例：「１月中」「来月末」「今週末」など）の場合は、そのままの表現で返すこと。不明な場合はnull）

【オーダ情報】
- order_number: オーダ番号（以下のいずれかのパターンに該当する場合のみ抽出）
  * パターン1: 990000～999999の数字のみの6桁（例：990015）
  * パターン2: 数字2桁+JP+数字6桁（例：24JP021670、25JP022318）
  * 該当しない場合はnull
- order_name: オーダ名（オーダ番号の後に続くテキスト。例：「SEJ_本対応_Google Cloud / Java バージョンアップ対応」「クラウドエース株式会社：定例会議（案件外）」など。オーダ番号がない場合はnull）

テキスト: {text}

必ず以下のJSON配列形式で返答してください（複数の依頼がある場合は配列に複数の要素を含める）:
[
    {{
        "target_name": "対象者の氏名またはnull",
        "target_email": "メールアドレスまたはnull",
        "tags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5"]（最大5つのタグの配列、不足する場合はnullで埋める。例：["アカウント管理", "アカウント新規申請登録", "スラック", null, null]）,
        "details": "概要・詳細またはnull",
        "due_date": "対応期日（明示的な日付の場合はYYYY-MM-DD形式、相対的な表現の場合はそのままの表現、不明な場合はnull）",
        "order_number": "オーダ番号（[990000～999999の数字6桁]または[数字2桁+JP+数字6桁]の形式、該当しない場合はnull）",
        "order_name": "オーダ名（オーダ番号の後に続くテキスト、オーダ番号がない場合はnull）"
    }}
]
"""
        
        from vertexai.generative_models import GenerationConfig
        
        generation_config = GenerationConfig(
            temperature=0,
            response_mime_type="application/json",
        )
        
        response = model.generate_content(
            prompt,
            generation_config=generation_config
        )
        
        # JSONレスポンスをパース
        result_text = response.text.strip()
        # コードブロックがある場合は除去
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
        result_text = result_text.strip()
        
        extracted_data_list = json.loads(result_text)
        
        # リストでない場合はリストに変換（後方互換性）
        if not isinstance(extracted_data_list, list):
            extracted_data_list = [extracted_data_list]
        
        # 期日を正規化
        for item in extracted_data_list:
            if "due_date" in item and item["due_date"]:
                normalized_date = normalize_due_date(item["due_date"])
                if normalized_date != item["due_date"]:
                    logger.info(f"期日を正規化: {item['due_date']} → {normalized_date}")
                item["due_date"] = normalized_date
        
        logger.info(f"抽出されたデータ（{len(extracted_data_list)}件）: {extracted_data_list}")
        return extracted_data_list
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        logger.error(f"Geminiでの情報抽出に失敗: {str(e)}")
        logger.error(f"エラー詳細:\n{error_detail}")
        raise


def _datetime_to_sheets_serial(dt: datetime) -> float:
    """
    datetimeをGoogleスプレッドシートのシリアル値（浮動小数点数）に変換する。
    スプレッドシートのエポックは1899-12-30。タイムゾーン情報は除去してローカル時刻として扱う。
    """
    epoch = datetime(1899, 12, 30)
    return (dt.replace(tzinfo=None) - epoch).total_seconds() / 86400


def _date_str_to_sheets_serial(date_str: str) -> Optional[int]:
    """
    YYYY-MM-DD形式の日付文字列をGoogleスプレッドシートのシリアル値（整数）に変換する。
    パースできない場合はNoneを返す。
    """
    try:
        from datetime import date as _date
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (_date(d.year, d.month, d.day) - _date(1899, 12, 30)).days
    except (ValueError, TypeError):
        return None


def write_to_spreadsheet(inquirer_name: str, extracted_data_list: List[Dict[str, Any]], 
                        slack_url: str, is_issue: bool = False) -> tuple[bool, List[int], List[int]]:
    """
    Googleスプレッドシートにデータを書き込む
    
    Args:
        inquirer_name: 問合せ者のユーザー名（表示名、実名、またはUser ID）
        extracted_data_list: 抽出された情報のリスト
        slack_url: 問合せ元のSlack URL
        is_issue: 課題管理モードの場合True（"課題管理"シートへA〜L列で書き込む）
        
    Returns:
        (書き込み成功時True, 書き込んだ行番号のリスト, 書き込んだ問合せNoのリスト)
    """
    try:
        # ADC (Application Default Credentials) を使用
        logger.info(f"スプレッドシートへの書き込み開始: SPREADSHEET_KEY={SPREADSHEET_KEY}, is_issue={is_issue}")
        credentials, _ = default()
        logger.info("認証情報の取得に成功")
        gc = gspread.authorize(credentials)
        logger.info("gspreadクライアントの初期化に成功")
        
        # スプレッドシートを開く
        logger.info(f"スプレッドシートを開く: KEY={SPREADSHEET_KEY}")
        spreadsheet = gc.open_by_key(SPREADSHEET_KEY)
        logger.info(f"スプレッドシートを開くことに成功: {spreadsheet.title}")
        sheet_name = "課題管理" if is_issue else "問合せリスト"
        worksheet = spreadsheet.worksheet(sheet_name)
        logger.info(f"ワークシートを取得: {worksheet.title}")
        
        # 問合せNoを取得（既存の最大値+1）
        # 1-2行目はヘッダーのため、3行目以降を確認
        existing_rows = worksheet.get_all_values()
        max_inquiry_no = 0
        if len(existing_rows) > 2:
            # 1列目（問合せNo）の最大値を取得
            for row in existing_rows[2:]:  # ヘッダー（1-2行目）をスキップ
                if row and row[0]:  # 1列目が存在する場合
                    try:
                        # 数値として解釈できるか確認
                        no_str = str(row[0]).strip()
                        if no_str.isdigit():
                            max_inquiry_no = max(max_inquiry_no, int(no_str))
                    except (ValueError, IndexError):
                        continue
        
        # タイムスタンプを取得（日本時間）
        jst = timezone(timedelta(hours=9))
        timestamp = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
        
        # 書き込むべき開始行を計算（3行目以降、既存データがある場合は最後の行の次）
        start_row = max(3, len(existing_rows) + 1)
        
        # 書き込んだ行番号を記録
        written_row_numbers = []
        
        # すべての行データを準備
        all_row_data = []
        for idx, extracted_data in enumerate(extracted_data_list):
            inquiry_no = max_inquiry_no + idx + 1
            
            if is_issue:
                # 課題管理用: A〜L列の12列構成
                raw_tags = extracted_data.get("tags", []) or []
                tags_str = ", ".join(t for t in raw_tags if t and t != "null")
                due_date_raw = extracted_data.get("due_date", "") or ""
                # 目標解決日: YYYY-MM-DD形式の場合はシリアル値、それ以外は文字列のまま
                due_date_serial = _date_str_to_sheets_serial(due_date_raw)
                due_date_value = due_date_serial if due_date_serial is not None else (
                    due_date_raw if due_date_raw not in ("null", "None", "") else ""
                )
                row_data = [
                    inquiry_no,                                          # A: 課題No
                    _datetime_to_sheets_serial(datetime.now(jst)),       # B: 起票日時（シリアル値）
                    inquirer_name,                                       # C: 起票者
                    slack_url,                                           # D: Slack URL
                    extracted_data.get("issue_title", ""),               # E: 課題タイトル
                    extracted_data.get("issue_summary", ""),             # F: 課題概要
                    tags_str,                                            # G: カテゴリ/タグ
                    extracted_data.get("priority", "") or "",            # H: 優先度
                    "",                                                  # I: 担当者（空）
                    due_date_value,                                      # J: 目標解決日（シリアル値または文字列）
                    "1.未着手",                                          # K: ステータス
                    "",                                                  # L: 最新状況メモ（空）
                ]
            else:
                # 問合せ管理用: A〜P列の16列構成（既存ロジック）
                tags = extracted_data.get("tags", [])
                tag_list = [tags[i] if i < len(tags) and tags[i] else "" for i in range(5)]
                row_data = [
                    inquiry_no,  # 問合せNo
                    timestamp,
                    inquirer_name,  # 問合せ者
                    extracted_data.get("details", ""),  # 概要・詳細
                    slack_url,  # 問合せ元Slack URL
                    tag_list[0],  # タグ1
                    tag_list[1],  # タグ2
                    tag_list[2],  # タグ3
                    tag_list[3],  # タグ4
                    tag_list[4],  # タグ5
                    extracted_data.get("target_name", ""),  # 【対象】氏名
                    extracted_data.get("target_email", ""),  # 【対象】Email
                    extracted_data.get("due_date", ""),  # 対応期日
                    extracted_data.get("order_number", ""),  # オーダ番号
                    extracted_data.get("order_name", ""),  # オーダ名
                    "1.未着手",  # P列: 進捗ステータス（初動）
                ]
            all_row_data.append(row_data)
            written_row_numbers.append(start_row + idx)
        
        # 3行目以降に一度に書き込む
        if all_row_data:
            last_col = "L" if is_issue else "P"
            range_name = f"A{start_row}:{last_col}{start_row + len(all_row_data) - 1}"
            worksheet.update(range_name, all_row_data, value_input_option='RAW')
            logger.info(f"スプレッドシートに書き込み成功: {len(all_row_data)}行を{start_row}行目から書き込み (シート: {sheet_name})")
            for idx, row_data in enumerate(all_row_data):
                logger.info(f"  No={row_data[0]}, 行={start_row + idx}")
        
        # 書き込んだ問合せNoのリストも返す
        written_inquiry_nos = [max_inquiry_no + idx + 1 for idx in range(len(extracted_data_list))]
        
        return True, written_row_numbers, written_inquiry_nos
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        logger.error(f"スプレッドシートへの書き込みに失敗: {str(e)}")
        logger.error(f"エラー詳細:\n{error_detail}")
        raise


def send_slack_reply(channel: str, thread_ts: str, message: str) -> None:
    """
    Slackのスレッドに返信を送信する
    
    Args:
        channel: チャンネルID
        thread_ts: スレッドのタイムスタンプ
        message: 送信するメッセージ
    """
    try:
        client = WebClient(token=SLACK_BOT_TOKEN)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=message
        )
        logger.info(f"Slackへの返信成功: {message}")
    except Exception as e:
        logger.error(f"Slackへの返信に失敗: {str(e)}")
        raise


REGISTERED_MARKER = "[要員追加登録済み]"


def handle_onboarding_flows(
    channel: str,
    event_ts: str,
    event_thread_ts: Optional[str],
    text: str,
    first_line: str,
    inquirer_name: str,
    user_id: Optional[str],
    slack_url: str,
) -> bool:
    """
    要員追加（権限パック）まわりの3つの入口を処理する。処理した場合は True、
    該当しない場合は False を返し、呼び出し側が従来の問合せ登録へ流す。

      1. 【要員追加】  : 依頼を解析し、権限ごとの登録内容をプレビュー返信（台帳には書かない）
      2. OK（スレ返信）: スレ内のプレビューを復元し、権限種別ごとに1行ずつ台帳へ登録
      3. 【完了連絡】N : 台帳の該当行を読み、依頼元スレッドで本人へ確認依頼をメンション
    """
    client = WebClient(token=SLACK_BOT_TOKEN)
    packs = onboarding.load_packs()

    # --- 3. 完了連絡 -------------------------------------------------
    if onboarding.is_completion_request(first_line):
        numbers = onboarding.parse_completion_numbers(text)
        if not numbers:
            send_slack_reply(channel, event_ts, "完了連絡の問合せNoを読み取れませんでした。例: 【完了連絡】問合せNo 101, 102")
            return True
        credentials, _ = default()
        gc = gspread.authorize(credentials)
        worksheet = gc.open_by_key(SPREADSHEET_KEY).worksheet("問合せリスト")
        rows = {str(r[0]).strip(): r for r in worksheet.get_all_values()[2:] if r and r[0]}
        results = []
        for no in numbers:
            row = rows.get(str(no))
            if not row:
                results.append(f"No{no}: 台帳に見つかりません")
                continue
            row = row + [""] * (16 - len(row))
            target = onboarding.parse_slack_url(row[4])
            name, email = row[10], row[11]
            mention = name or "ご担当者"
            uid = None
            if email:
                try:
                    uid = client.users_lookupByEmail(email=email)["user"]["id"]
                except Exception as e:
                    # users:read.email が無い環境では、氏名での一意照合に切り替える
                    logger.warning(f"メールからSlackユーザーを特定できません。氏名で照合します: {e}")
                    try:
                        members, cursor = [], None
                        for _ in range(20):
                            resp = client.users_list(limit=200, cursor=cursor)
                            members += resp["members"]
                            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                            if not cursor:
                                break
                        uid = onboarding.match_user_by_name(members, name, email)
                    except Exception as e2:
                        logger.warning(f"氏名での照合にも失敗: {e2}")
            if uid:
                mention = f"<@{uid}>"
            if not target:
                results.append(f"No{no}: 依頼元のSlack URLを読み取れず、連絡できません")
                continue
            try:
                client.chat_postMessage(
                    channel=target["channel"],
                    thread_ts=target["thread_ts"],
                    text=onboarding.build_completion_message(row, packs, mention),
                )
                results.append(f"No{no}（{row[7] or '権限'}）: 連絡しました")
            except Exception as e:
                logger.error(f"完了連絡の投稿に失敗 No{no}: {e}")
                results.append(f"No{no}: 連絡の投稿に失敗しました（{e}）")
        send_slack_reply(channel, event_ts, "完了連絡の結果:\n" + "\n".join(results))
        return True

    # --- 1. 要員追加の依頼 → プレビュー ---------------------------------
    if onboarding.is_onboarding_request(first_line):
        person = onboarding.parse_onboarding_request(text)
        if person["missing"]:
            send_slack_reply(
                channel, event_ts,
                "要員追加の依頼に、次の情報が足りません: " + "、".join(person["missing"]) +
                "\n例:\n【要員追加】\n氏名: 酒谷 尋\nメール: sakatani.jin@cloud-ace.jp\nチーム: インフラ\n期日: 11/2",
            )
            return True
        exclusions = onboarding.parse_exclusions(text)
        entries = onboarding.expand_ledger_entries(person, packs, exclusions)
        if not onboarding.is_direct_confirm(text):
            send_slack_reply(channel, event_ts, onboarding.build_preview_message(person, entries, packs))
            return True
        # 「確定」行つき: プレビューなしで登録（スレッド履歴の権限が無くても動く）。
        # 二重登録防止は、台帳に同じメールの【要員追加】行があるかで見る。
        credentials, _ = default()
        ws = gspread.authorize(credentials).open_by_key(SPREADSHEET_KEY).worksheet("問合せリスト")
        dup = [str(r[0]) for r in ws.get_all_values()[2:]
               if len(r) > 11 and r[11].strip().lower() == person["email"].lower() and "【要員追加】" in r[3]]
        if dup:
            send_slack_reply(channel, event_ts, f"{person['name']}さんの要員追加は、すでに台帳に登録されています（問合せNo: {', '.join(dup)}）。二重登録はしません。")
            return True
        success, rows_written, nos = write_to_spreadsheet(inquirer_name, entries, slack_url, is_issue=False)
        mention_text = f"<@{user_id}> " if user_id else ""
        lines = [f"{mention_text}{REGISTERED_MARKER} {person['name']}さんの権限を、権限種別ごとに{len(entries)}件登録しました。"]
        for e, no in zip(entries, nos):
            lines.append(f"問合せNo{no}: {e['tags'][2]}")
        send_slack_reply(channel, event_ts, "\n".join(lines))
        return True

    # --- 2. OK 返信 → 台帳へ登録 -----------------------------------------
    if event_thread_ts and event_thread_ts != event_ts and onboarding.is_confirmation(text):
        try:
            messages = client.conversations_replies(channel=channel, ts=event_thread_ts, limit=200)["messages"]
        except Exception as e:
            send_slack_reply(
                channel, event_thread_ts,
                f"スレッドの内容を読み取れませんでした（{e}）。Slack App の history 権限（channels:history / groups:history）を確認してください。",
            )
            return True
        person = onboarding.find_preview_in_thread(messages)
        if not person:
            return False  # プレビューが無いスレッドの OK は、従来の問合せ登録に流す
        if any(REGISTERED_MARKER in (m.get("text") or "") for m in messages):
            send_slack_reply(channel, event_thread_ts, "このスレッドの要員追加は登録済みです。二重登録はしません。")
            return True
        exclusions = onboarding.parse_exclusions(text)
        entries = onboarding.expand_ledger_entries(person, packs, exclusions)
        parent_url = slack_url.rsplit("/p", 1)[0] + "/p" + event_thread_ts.replace(".", "") if slack_url else ""
        success, rows_written, nos = write_to_spreadsheet(inquirer_name, entries, parent_url, is_issue=False)
        mention_text = f"<@{user_id}> " if user_id else ""
        lines = [f"{mention_text}{REGISTERED_MARKER} {person['name']}さんの権限を、権限種別ごとに{len(entries)}件登録しました。"]
        if exclusions:
            lines.append(f"（除外指定: {', '.join(exclusions)}）")
        for e, no in zip(entries, nos):
            lines.append(f"問合せNo{no}: {e['tags'][2]}")
        send_slack_reply(channel, event_thread_ts, "\n".join(lines))
        return True

    return False


@functions_framework.http
def slack_bot_handler(request: Request) -> tuple[str, int]:
    """
    Cloud Functions のエントリーポイント
    
    Args:
        request: Flask Request オブジェクト
        
    Returns:
        (レスポンス本文, HTTPステータスコード) のタプル
    """
    try:
        # Slackリトライ対策: X-Slack-Retry-Num ヘッダーが存在する場合は即座に200を返す
        if request.headers.get("X-Slack-Retry-Num"):
            logger.info("Slackリトライリクエストを検出。処理をスキップします。")
            return ("", 200)
        
        # リクエストボディを取得（署名検証の前に取得する必要がある）
        # 注意: get_data(cache=True)を使用してストリームをキャッシュ
        request_body = request.get_data(cache=True)
        payload = request.get_json(silent=True)
        
        # URL検証 (url_verification) - 署名検証の前に処理する必要がある
        if payload and payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            logger.info("URL検証リクエストを受信")
            if challenge:
                return (challenge, 200)
            else:
                logger.warning("URL検証リクエストにchallengeが含まれていません")
                return ("Missing challenge", 400)
        
        # URL検証でない場合のみ署名検証を実行
        if not payload:
            logger.warning("リクエストボディが取得できませんでした")
            return ("Invalid request", 400)
        
        # 署名検証（url_verification以外のリクエストに対して）
        if signature_verifier:
            if not signature_verifier.is_valid(
                body=request_body,
                timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
                signature=request.headers.get("X-Slack-Signature", "")
            ):
                logger.warning("署名検証に失敗しました")
                return ("Invalid signature", 401)
        
        # イベントタイプの確認
        if payload.get("type") != "event_callback":
            logger.info(f"未対応のイベントタイプ: {payload.get('type')}")
            return ("OK", 200)
        
        event = payload.get("event", {})
        event_type = event.get("type")
        
        # メンションイベントの処理
        if event_type == "app_mention":
            channel = event.get("channel")
            thread_ts = event.get("ts")
            text = event.get("text", "")
            user_id = event.get("user")
            team_id = payload.get("team_id", "")  # team_idを取得
            
            # ボットへのメンション部分を除去（例: "<@U123456> " を除去）
            text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
            text = normalize_slack_mailto_links(text)

            # 課題管理キーワードの判定（1行目のみを対象とする）
            # 本文中に【課題】等が含まれる誤検知を防ぐため、先頭行にキーワードがある場合のみ課題と判定
            ISSUE_KEYWORDS = ["【課題事項】", "【課題】", "【SEJ案件課題】","課題：","課題事項：",
                              "[課題事項]", "[課題]", "[SEJ案件課題]"]
            first_line = text.splitlines()[0] if text else ""
            is_issue = any(kw in first_line for kw in ISSUE_KEYWORDS)
            error_id = generate_error_id()
            logger.info(f"メンションを受信 error_id={error_id} (is_issue={is_issue}): {text}")
            
            # 文字数チェック（1000文字以内のみ受け付け）
            if len(text) > 1000:
                error_message = f"お問合せの内容が長すぎます（{len(text)}文字）。1000文字以内で再度入力してください。"
                send_slack_reply(channel, thread_ts, error_message)
                logger.warning(f"文字数超過: {len(text)}文字")
                return ("OK", 200)
            
            # 問合せ者の特定（User IDからユーザー名を取得）
            inquirer_name = "不明"
            if user_id:
                try:
                    client = WebClient(token=SLACK_BOT_TOKEN)
                    user_info = client.users_info(user=user_id)
                    # 表示名を優先、なければ実名、それもなければUser IDを使用
                    inquirer_name = (
                        user_info["user"].get("profile", {}).get("display_name") or
                        user_info["user"].get("real_name") or
                        user_info["user"].get("name") or
                        user_id
                    )
                    logger.info(f"問合せ者を特定: {inquirer_name} (User ID: {user_id})")
                except Exception as e:
                    logger.warning(f"ユーザー情報の取得に失敗したため、User IDを使用: {str(e)}")
                    inquirer_name = user_id
                    logger.info(f"問合せ者を特定: User ID {inquirer_name}")
            else:
                logger.warning("User IDが取得できませんでした")
            
            # 問合せ元のSlack URLを生成
            # SlackのメッセージURL形式: https://{workspace}.slack.com/archives/{channel}/p{ts}
            # tsは小数点を含むタイムスタンプ（例: 1234567890.123456）を、小数点を削除して使用
            slack_url = ""
            if channel and thread_ts:
                ts_for_url = thread_ts.replace(".", "")
                # workspace名を取得するためにSlack Web APIを使用
                try:
                    client = WebClient(token=SLACK_BOT_TOKEN)
                    team_info = client.team_info(team=team_id)
                    workspace_domain = team_info["team"]["domain"]
                    # 正しいメッセージURL形式: https://{workspace}.slack.com/archives/{channel}/p{ts}
                    slack_url = f"https://{workspace_domain}.slack.com/archives/{channel}/p{ts_for_url}"
                    logger.info(f"Slack URL生成: {slack_url}")
                except Exception as e:
                    logger.warning(f"workspace名の取得に失敗したため、app.slack.com形式を使用: {str(e)}")
                    # フォールバック: app.slack.com形式（ブラウザでは開けないが、Slackアプリ内では動作する可能性がある）
                    slack_url = f"https://app.slack.com/client/{team_id}/{channel}/p{ts_for_url}"
                    logger.info(f"Slack URL生成（フォールバック）: {slack_url}")
            
            # 要員追加（権限パック）フロー: 専用キーワード / スレ内プレビューがあるときだけ動く。
            # 該当しない場合は False が返り、下の従来の問合せ登録にそのまま流れる。
            if not is_issue:
                try:
                    if handle_onboarding_flows(
                        channel, thread_ts, event.get("thread_ts"), text, first_line,
                        inquirer_name, user_id, slack_url,
                    ):
                        return ("OK", 200)
                except Exception as e:
                    import traceback
                    logger.error(f"要員追加フローでエラー error_id={error_id}: {e}\n{traceback.format_exc()}")
                    try:
                        send_slack_reply(channel, thread_ts, f"要員追加の処理でエラーが発生しました（エラーID: {error_id}）。内容を確認します。")
                    except Exception:
                        pass
                    return ("OK", 200)

            # Geminiで情報抽出
            try:
                extracted_data_list = extract_info_with_gemini(text, inquirer_name, is_issue=is_issue)
                
                # バリデーション: リストが空でないこと
                if not extracted_data_list:
                    error_message = "情報を正しく読み取れませんでした。再度入力してください"
                    send_slack_reply(channel, thread_ts, error_message)
                    return ("OK", 200)

                # メールアドレスの正規表現フォールバック（問合せ管理モード時）
                # Gemini が target_email を null で返した場合でも、本文にメールがあれば補完
                if not is_issue:
                    extracted_data_list = fallback_target_email_with_regex(extracted_data_list, text)
                
                # バリデーション: 問合せ管理モードでタグに「アカウント管理」が含まれる場合のみ、target_emailが必須
                # ただし1行目にメールアドレス不要・アカウント管理ではない旨の記載がある場合はスキップ
                NO_EMAIL_REQUIRED_KEYWORDS = [
                    "メールアドレス無し", "メールアドレスは不要", "メールアドレスは有りません",
                    "メールアドレスはありません", "アカウント管理ではない", "アカウント管理ではありません",
                ]
                skip_email_validation = any(kw in first_line for kw in NO_EMAIL_REQUIRED_KEYWORDS)
                if not is_issue and not skip_email_validation:
                    for item in extracted_data_list:
                        tags = item.get("tags", [])
                        if isinstance(tags, list) and "アカウント管理" in tags:
                            if not item.get("target_email"):
                                error_message = "アカウント管理の依頼には対象者のメールアドレスが必要です。メールアドレスを含めて再度入力してください"
                                send_slack_reply(channel, thread_ts, error_message)
                                return ("OK", 200)
                
                # スプレッドシートに書き込み（複数件対応）
                success, written_row_numbers, written_inquiry_nos = write_to_spreadsheet(
                    inquirer_name, extracted_data_list, slack_url, is_issue=is_issue
                )
                
                # スプレッドシートの範囲リンクを生成
                sheet_name_for_link = "課題管理" if is_issue else "問合せリスト"
                last_col_for_link = "L" if is_issue else "P"
                try:
                    credentials, _ = default()
                    gc = gspread.authorize(credentials)
                    spreadsheet = gc.open_by_key(SPREADSHEET_KEY)
                    worksheet = spreadsheet.worksheet(sheet_name_for_link)
                    gid = worksheet.id
                except:
                    gid = 0
                
                spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_KEY}/edit"
                sheet_links = []
                if written_row_numbers and written_inquiry_nos:
                    min_row = min(written_row_numbers)
                    max_row = max(written_row_numbers)
                    min_inquiry_no = min(written_inquiry_nos)
                    max_inquiry_no = max(written_inquiry_nos)
                    no_label = "課題No" if is_issue else "問合せNo"
                    
                    if min_row == max_row:
                        range_link = f"{spreadsheet_url}#gid={gid}&range=A{min_row}:{last_col_for_link}{min_row}"
                        sheet_links.append(f"<{range_link}|{no_label}{min_inquiry_no}>")
                    else:
                        range_link = f"{spreadsheet_url}#gid={gid}&range=A{min_row}:{last_col_for_link}{max_row}"
                        sheet_links.append(f"<{range_link}|{no_label}{min_inquiry_no}-{max_inquiry_no}>")
                
                # 成功メッセージを作成（問合せ者へのメンションを追加）
                mention_text = f"<@{user_id}> " if user_id else ""
                count = len(extracted_data_list)
                
                if is_issue:
                    success_message = (
                        f"{mention_text}ご報告いただいた課題について、以下の課題管理シートに{count}件登録しました。"
                        f"内容をご確認の上、必要に応じて担当者のアサインをお願いします。\n"
                    )
                else:
                    success_message = (
                        f"{mention_text}お問合せ頂いた内容について、以下の通りスプレッドシートに{count}件登録しました。"
                        f"認識相違が無いかご確認ください。\n"
                    )
                
                if sheet_links:
                    success_message += f"\n📋 スプレッドシート: {', '.join(sheet_links)}\n"
                
                for idx, item in enumerate(extracted_data_list, 1):
                    entry_no = written_inquiry_nos[idx - 1] if idx <= len(written_inquiry_nos) else ""
                    no_label = "課題No" if is_issue else "問合せNo"
                    success_message += f"\n【{idx}件目】"
                    if entry_no:
                        success_message += f" ({no_label}: {entry_no})"
                    success_message += "\n"
                    
                    if is_issue:
                        if item.get("issue_title"):
                            success_message += f"課題タイトル: {item.get('issue_title')}\n"
                        raw_tags = item.get("tags", []) or []
                        valid_tags = [t for t in raw_tags if t and t != "null"]
                        if valid_tags:
                            success_message += f"カテゴリ/タグ: {', '.join(valid_tags)}\n"
                        if item.get("priority"):
                            success_message += f"優先度: {item.get('priority')}\n"
                        if item.get("due_date"):
                            success_message += f"目標解決日: {item.get('due_date')}\n"
                    else:
                        if item.get("target_name"):
                            success_message += f"対象者: {item.get('target_name')}\n"
                        if item.get("target_email"):
                            success_message += f"メールアドレス: {item.get('target_email')}\n"
                        if item.get("due_date"):
                            success_message += f"対応期日: {item.get('due_date')}\n"
                        tags = item.get("tags", [])
                        if isinstance(tags, list) and tags:
                            valid_tags = [tag for tag in tags if tag and tag != "null"]
                            if valid_tags:
                                success_message += f"タグ: {', '.join(valid_tags)}\n"
                
                send_slack_reply(channel, thread_ts, success_message)
                
            except Exception as e:
                import traceback
                error_detail = traceback.format_exc()
                stage = classify_error_stage(error_detail)
                logger.error(
                    f"処理中にエラーが発生 error_id={error_id} stage={stage} "
                    f"revision={CLOUD_RUN_REVISION}: {str(e)}"
                )
                logger.error(f"エラー詳細 error_id={error_id}:\n{error_detail}")
                error_message = build_slack_error_message(error_id, stage, e)
                try:
                    send_slack_reply(channel, thread_ts, error_message)
                except Exception as reply_error:
                    logger.error(
                        f"エラー返信に失敗 error_id={error_id}: {str(reply_error)}"
                    )
                return ("OK", 200)
        
        return ("OK", 200)
        
    except Exception as e:
        unexpected_id = generate_error_id()
        logger.error(f"予期しないエラー error_id={unexpected_id}: {str(e)}")
        return ("Internal Server Error", 500)
