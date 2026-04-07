"""
Slack アカウント申請自動化システム
Cloud Functions (第2世代) 用エントリーポイント
"""
import os
import json
import logging
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

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数の取得
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SPREADSHEET_KEY = os.environ.get("SPREADSHEET_KEY")
PROJECT_ID = os.environ.get("PROJECT_ID")
# デフォルトはasia-northeast1（日本リージョン）
# 環境変数が設定されていない場合は、Cloud Runのリージョンに合わせる
LOCATION = os.environ.get("LOCATION", "asia-northeast1")

# 署名検証用のインスタンス
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET) if SLACK_SIGNING_SECRET else None


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
        # Vertex AI の初期化
        logger.info(f"Vertex AI初期化: PROJECT_ID={PROJECT_ID}, LOCATION={LOCATION}")
        aiplatform.init(project=PROJECT_ID, location=LOCATION)
        
        from vertexai.generative_models import GenerativeModel
        
        # モデル名: gemini-2.5-flash-lite を使用（Gemini 1.5は廃止済み）
        # リージョンによって利用可能なモデル名が異なる場合があります
        model_name = "gemini-2.5-flash-lite"
        logger.info(f"使用するモデル: {model_name} (リージョン: {LOCATION})")
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
            logger.info(f"メンションを受信 (is_issue={is_issue}): {text}")
            
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
            
            # Geminiで情報抽出
            try:
                extracted_data_list = extract_info_with_gemini(text, inquirer_name, is_issue=is_issue)
                
                # バリデーション: リストが空でないこと
                if not extracted_data_list:
                    error_message = "情報を正しく読み取れませんでした。再度入力してください"
                    send_slack_reply(channel, thread_ts, error_message)
                    return ("OK", 200)
                
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
                logger.error(f"処理中にエラーが発生: {str(e)}")
                logger.error(f"エラー詳細:\n{error_detail}")
                error_message = "エラーが発生しました。しばらく時間をおいて再度お試しください。"
                try:
                    send_slack_reply(channel, thread_ts, error_message)
                except:
                    pass
                return ("OK", 200)
        
        return ("OK", 200)
        
    except Exception as e:
        logger.error(f"予期しないエラー: {str(e)}")
        return ("Internal Server Error", 500)
