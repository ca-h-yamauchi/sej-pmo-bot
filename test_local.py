"""
ローカルテスト用スクリプト

使用方法:
1. 環境変数を設定（.envファイルまたは直接設定）
2. python test_local.py を実行

注意: python-dotenvを使用する場合は、以下をインストール:
    pip install python-dotenv
"""
import os
import json
import hmac
import hashlib
import time
import requests

# python-dotenvを使用する場合（オプション）
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ .envファイルから環境変数を読み込みました")
except ImportError:
    print("ℹ python-dotenvがインストールされていません。環境変数を直接設定してください。")
    print("  インストール: pip install python-dotenv")

def generate_slack_signature(body: str, timestamp: str, signing_secret: str) -> str:
    """Slack署名を生成"""
    sig_basestring = f"v0:{timestamp}:{body}"
    signature = hmac.new(
        signing_secret.encode('utf-8'),
        sig_basestring.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return f"v0={signature}"

def test_url_verification(base_url: str = "http://localhost:8080"):
    """URL検証のテスト"""
    print("\n=== URL検証テスト ===")
    
    payload = {
        "type": "url_verification",
        "challenge": "test-challenge-12345"
    }
    
    response = requests.post(
        base_url,
        headers={"Content-Type": "application/json"},
        json=payload
    )
    
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    
    if response.status_code == 200 and response.text == "test-challenge-12345":
        print("✓ URL検証テスト成功")
        return True
    else:
        print("✗ URL検証テスト失敗")
        return False

def test_app_mention(base_url: str = "http://localhost:8080"):
    """メンションイベントのテスト"""
    print("\n=== メンションイベントテスト ===")
    
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not signing_secret:
        print("✗ SLACK_SIGNING_SECRETが設定されていません")
        return False
    
    timestamp = str(int(time.time()))
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C1234567890",
            "ts": "1234567890.123456",
            "text": "<@U123456> 佐藤太郎さんのアカウント作成をお願い。営業部、sato@example.com"
        }
    }
    
    body = json.dumps(payload)
    signature = generate_slack_signature(body, timestamp, signing_secret)
    
    headers = {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature
    }
    
    try:
        response = requests.post(
            base_url,
            headers=headers,
            data=body
        )
        
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code == 200:
            print("✓ メンションイベントテスト成功（レスポンス200）")
            print("  注意: 実際のGemini APIとSlack APIは呼び出されません")
            return True
        else:
            print(f"✗ メンションイベントテスト失敗（ステータス: {response.status_code}）")
            return False
            
    except requests.exceptions.ConnectionError:
        print("✗ 接続エラー: Functions Frameworkサーバーが起動していません")
        print("  以下のコマンドでサーバーを起動してください:")
        print("  functions-framework --target=slack_bot_handler --port=8080")
        return False
    except Exception as e:
        print(f"✗ エラーが発生しました: {str(e)}")
        return False

def test_retry_header(base_url: str = "http://localhost:8080"):
    """リトライヘッダーのテスト"""
    print("\n=== リトライヘッダーテスト ===")
    
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C1234567890",
            "ts": "1234567890.123456",
            "text": "<@U123456> テストメッセージ"
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Slack-Retry-Num": "1"
    }
    
    try:
        response = requests.post(
            base_url,
            headers=headers,
            json=payload
        )
        
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code == 200 and response.text == "":
            print("✓ リトライヘッダーテスト成功（処理がスキップされました）")
            return True
        else:
            print("✗ リトライヘッダーテスト失敗")
            return False
            
    except requests.exceptions.ConnectionError:
        print("✗ 接続エラー: Functions Frameworkサーバーが起動していません")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("Slack Bot ローカルテスト")
    print("=" * 50)
    
    # 環境変数の確認
    required_vars = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SPREADSHEET_KEY", "PROJECT_ID"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"\n⚠ 以下の環境変数が設定されていません: {', '.join(missing_vars)}")
        print("環境変数を設定してから再度実行してください。")
    else:
        print("\n✓ 必要な環境変数が設定されています")
    
    base_url = os.environ.get("TEST_URL", "http://localhost:8080")
    print(f"\nテスト対象URL: {base_url}")
    
    # テスト実行
    results = []
    results.append(test_url_verification(base_url))
    results.append(test_retry_header(base_url))
    
    # メンションイベントテストは署名が必要なので、環境変数が設定されている場合のみ実行
    if os.environ.get("SLACK_SIGNING_SECRET"):
        results.append(test_app_mention(base_url))
    else:
        print("\n⚠ SLACK_SIGNING_SECRETが設定されていないため、メンションイベントテストをスキップします")
    
    # 結果サマリー
    print("\n" + "=" * 50)
    print("テスト結果サマリー")
    print("=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"成功: {passed}/{total}")
    
    if passed == total:
        print("✓ すべてのテストが成功しました！")
    else:
        print("✗ 一部のテストが失敗しました。ログを確認してください。")
