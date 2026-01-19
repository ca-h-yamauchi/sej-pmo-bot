"""
Slack アカウント申請自動化システム
Cloud Functions (第2世代) 用エントリーポイント
"""
import os
import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
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
    
    now = datetime.now()
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


def extract_info_with_gemini(text: str, inquirer_name: str) -> List[Dict[str, Any]]:
    """
    Vertex AI (Gemini 2.5 Flash Lite) を使用してテキストから情報を抽出する
    
    Args:
        text: 抽出対象のテキスト
        inquirer_name: 問合せ者のユーザー名（表示名、実名、またはUser ID）
        
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

【タグ情報】
この問い合わせの属性を表すタグを最大5つまで設定してください。
- タグの例：「アカウント管理」「アカウント新規申請登録」「スラック」「課題」「作業依頼」など、問い合わせの種類や内容を表すタグ
- 1つの問い合わせに対して複数のタグを設定できます
  * 例1：アカウント管理の問い合わせ → ["アカウント管理", "アカウント新規申請登録", null, null, null]
  * 例2：Slackに関する改善したい事項の問い合わせ → ["課題", "Slack", null, null, null]
  * 例3：アカウント管理でSlack関連、権限の追加に関する問い合わせ → ["アカウント管理", "権限追加", "Slack", null, null]
- 重要：問い合わせ内に所属を表す情報（例：「営業のAさん」「SREチームのBさん」「コンサルティング部のCさん」など）が明示的に含まれている場合は、その所属情報もタグに含めてください。
  * 例：「営業のAさんのAsanaアカウント追加」→ ["アカウント管理", "新規登録", "Asana", "営業", null]
  * 例：「SREチームのBさんのSlackアカウント作成」→ ["アカウント管理", "新規登録", "スラック", "SREチーム", null]
- tags: タグの配列（最大5つ、不足する場合はnullで埋める。必ず5つの要素を持つ配列として返すこと）

【その他】
- details: 概要・詳細（不明な場合はnull）
- due_date: 対応期日（作業して欲しい期日が明示されている場合のみ記載。明示的な日付（例：「2024-01-31」）の場合はYYYY-MM-DD形式で返す。相対的な表現（例：「１月中」「来月末」「今週末」など）の場合は、そのままの表現で返すこと。不明な場合はnull）

テキスト: {text}

必ず以下のJSON配列形式で返答してください（複数の依頼がある場合は配列に複数の要素を含める）:
[
    {{
        "target_name": "対象者の氏名またはnull",
        "target_email": "メールアドレスまたはnull",
        "tags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5"]（最大5つのタグの配列、不足する場合はnullで埋める。例：["アカウント管理", "アカウント新規申請登録", "スラック", null, null]）,
        "details": "概要・詳細またはnull",
        "due_date": "対応期日（明示的な日付の場合はYYYY-MM-DD形式、相対的な表現の場合はそのままの表現、不明な場合はnull）"
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


def write_to_spreadsheet(inquirer_name: str, extracted_data_list: List[Dict[str, Any]], 
                        original_message: str, slack_url: str) -> tuple[bool, List[int]]:
    """
    Googleスプレッドシートにデータを書き込む
    
    Args:
        inquirer_name: 問合せ者のユーザー名（表示名、実名、またはUser ID）
        extracted_data_list: 抽出された情報のリスト
        original_message: 元のメッセージ
        slack_url: 問合せ元のSlack URL
        
    Returns:
        (書き込み成功時True, 書き込んだ行番号のリスト, 書き込んだ問合せNoのリスト)
    """
    try:
        # ADC (Application Default Credentials) を使用
        logger.info(f"スプレッドシートへの書き込み開始: SPREADSHEET_KEY={SPREADSHEET_KEY}")
        credentials, _ = default()
        logger.info("認証情報の取得に成功")
        gc = gspread.authorize(credentials)
        logger.info("gspreadクライアントの初期化に成功")
        
        # スプレッドシートを開く
        logger.info(f"スプレッドシートを開く: KEY={SPREADSHEET_KEY}")
        spreadsheet = gc.open_by_key(SPREADSHEET_KEY)
        logger.info(f"スプレッドシートを開くことに成功: {spreadsheet.title}")
        worksheet = spreadsheet.sheet1
        logger.info(f"ワークシートを取得: {worksheet.title}")
        
        # 問合せNoを取得（既存の最大値+1）
        # 1行目はヘッダーのため、2行目以降を確認
        existing_rows = worksheet.get_all_values()
        max_inquiry_no = 0
        if len(existing_rows) > 1:
            # 1列目（問合せNo）の最大値を取得
            for row in existing_rows[1:]:  # ヘッダーをスキップ
                if row and row[0]:  # 1列目が存在する場合
                    try:
                        # 数値として解釈できるか確認
                        no_str = str(row[0]).strip()
                        if no_str.isdigit():
                            max_inquiry_no = max(max_inquiry_no, int(no_str))
                    except (ValueError, IndexError):
                        continue
        
        # タイムスタンプを取得
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 書き込んだ行番号を記録
        written_row_numbers = []
        
        # リストの各要素を行として追加
        for idx, extracted_data in enumerate(extracted_data_list):
            inquiry_no = max_inquiry_no + idx + 1
            
            # タグを取得（配列形式、最大5つ）
            tags = extracted_data.get("tags", [])
            # タグを5つに揃える（不足する場合は空文字で埋める）
            tag_list = [tags[i] if i < len(tags) and tags[i] else "" for i in range(5)]
            
            row_data = [
                inquiry_no,  # 問合せNo
                timestamp,
                inquirer_name,  # 問合せ者
                slack_url,  # 問合せ元Slack URL
                tag_list[0],  # タグ1
                tag_list[1],  # タグ2
                tag_list[2],  # タグ3
                tag_list[3],  # タグ4
                tag_list[4],  # タグ5
                extracted_data.get("target_name", ""),  # 【対象】氏名
                extracted_data.get("target_email", ""),  # 【対象】Email
                extracted_data.get("due_date", ""),  # 対応期日
                extracted_data.get("details", ""),  # 概要・詳細
                original_message  # 元のメッセージ
            ]
            
            worksheet.append_row(row_data)
            # 書き込んだ行番号を取得（現在の行数）
            written_row = len(existing_rows) + len(written_row_numbers) + 1
            written_row_numbers.append(written_row)
            logger.info(f"スプレッドシートに書き込み成功: 問合せNo={inquiry_no}, 行={written_row}, {row_data}")
        
        # 書き込んだ問合せNoのリストも返す
        written_inquiry_nos = []
        for idx in range(len(extracted_data_list)):
            inquiry_no = max_inquiry_no + idx + 1
            written_inquiry_nos.append(inquiry_no)
        
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
            
            logger.info(f"メンションを受信: {text}")
            
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
                extracted_data_list = extract_info_with_gemini(text, inquirer_name)
                
                # バリデーション: リストが空でないこと
                if not extracted_data_list:
                    error_message = "情報を正しく読み取れませんでした。再度入力してください"
                    send_slack_reply(channel, thread_ts, error_message)
                    return ("OK", 200)
                
                # バリデーション: タグに「アカウント管理」が含まれる場合のみ、target_emailが必須
                for item in extracted_data_list:
                    tags = item.get("tags", [])
                    if isinstance(tags, list) and "アカウント管理" in tags:
                        if not item.get("target_email"):
                            error_message = "アカウント管理の依頼には対象者のメールアドレスが必要です。メールアドレスを含めて再度入力してください"
                            send_slack_reply(channel, thread_ts, error_message)
                            return ("OK", 200)
                
                # スプレッドシートに書き込み（複数件対応）
                success, written_row_numbers, written_inquiry_nos = write_to_spreadsheet(inquirer_name, extracted_data_list, text, slack_url)
                
                # スプレッドシートの範囲リンクを生成
                # gidを取得（sheet1のgidは通常0だが、確認する）
                try:
                    credentials, _ = default()
                    gc = gspread.authorize(credentials)
                    spreadsheet = gc.open_by_key(SPREADSHEET_KEY)
                    worksheet = spreadsheet.sheet1
                    gid = worksheet.id  # gspreadのidプロパティでgidを取得
                except:
                    gid = 0  # デフォルト値
                
                spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_KEY}/edit"
                sheet_links = []
                if written_row_numbers and written_inquiry_nos:
                    # 書き込んだ行範囲のリンクを生成
                    min_row = min(written_row_numbers)
                    max_row = max(written_row_numbers)
                    min_inquiry_no = min(written_inquiry_nos)
                    max_inquiry_no = max(written_inquiry_nos)
                    
                    if min_row == max_row:
                        # 1行のみの場合
                        range_link = f"{spreadsheet_url}#gid={gid}&range=A{min_row}:N{min_row}"
                        sheet_links.append(f"<{range_link}|問合せNo{min_inquiry_no}>")
                    else:
                        # 複数行の場合
                        range_link = f"{spreadsheet_url}#gid={gid}&range=A{min_row}:N{max_row}"
                        sheet_links.append(f"<{range_link}|問合せNo{min_inquiry_no}-{max_inquiry_no}>")
                
                # 成功メッセージを作成
                success_message = f"お問合せ頂いた内容について、以下の通りスプレッドシートに{len(extracted_data_list)}件登録しました。認識相違が無いかご確認ください。\n"
                
                # スプレッドシートリンクを追加
                if sheet_links:
                    success_message += f"\n📋 スプレッドシート: {', '.join(sheet_links)}\n"
                
                for idx, item in enumerate(extracted_data_list, 1):
                    inquiry_no = written_inquiry_nos[idx - 1] if idx <= len(written_inquiry_nos) else ""
                    success_message += f"\n【{idx}件目】"
                    if inquiry_no:
                        success_message += f" (問合せNo: {inquiry_no})"
                    success_message += "\n"
                    if item.get("target_name"):
                        success_message += f"対象者: {item.get('target_name')}\n"
                    if item.get("target_email"):
                        success_message += f"メールアドレス: {item.get('target_email')}\n"
                    if item.get("due_date"):
                        success_message += f"対応期日: {item.get('due_date')}\n"
                    # タグを表示
                    tags = item.get("tags", [])
                    if isinstance(tags, list) and tags:
                        # nullや空文字を除外してタグを表示
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
