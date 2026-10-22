# SEJ-PMO Bot

CA 社内向け Slack ボット。メンションされた依頼を Vertex AI（Gemini）で構造化し、SEJ-PMO の問合せ管理スプレッドシートへ登録する。

**利用者向け**: [documents/利用マニュアル.md](documents/利用マニュアル.md)（人間向け HTML は同フォルダの `利用マニュアル.html`。Cursor のリンクではなくブラウザで開く）

## できること

- Slack メンションで **問合せ**（アカウント・作業依頼）または **課題** を登録する
- 1通で複数件を行分割する
- 対応期日の相対表現（「来週末」など）を日付に寄せる
- 失敗時は Slack に `error_id` を返し、Cloud Logging と突き合わせる

権限の付与・削除そのものは行わない。台帳は社内専用で、問合せ No. は SEJ に出さない。

## 本番

| 項目 | 値 |
|---|---|
| コード正本 | このリポジトリの `main` |
| GitHub | https://github.com/ca-h-yamauchi/sej-pmo-bot |
| GCP | `test-yama-haj-2025` |
| Cloud Run | `slack-bot-handler-us` / **us-central1** |
| URL | https://slack-bot-handler-us-949120639074.us-central1.run.app |
| エントリ | `slack_bot_handler`（Python 3.11） |
| モデル | `gemini-3.5-flash-lite`（`LOCATION=global`。環境変数 `GEMINI_MODEL` で切替可） |
| Slack トークン | Secret Manager（平文の環境変数に置かない） |
| シート | 環境変数 `SPREADSHEET_KEY`（問合せリスト / 課題管理） |

`asia-northeast1` やサービス名 `slack-bot-handler`（接尾辞なし）へはデプロイしない。

## リポジトリ構成

```
main.py                 Cloud Run の本体
requirements.txt
cloudbuild.yaml         ソースデプロイ定義（env / Secret は上書きしない）
env.example            ローカル用の変数名のみ（実トークンは入れない）
.github/workflows/      main push で本番デプロイ
cloud_run_files/        手動デプロイと IAM 補助
documents/              利用・設計・デプロイ手順
```

`.env` と `cloud_run_files/cloud_run.yaml` は Git に含めない。

## 開発（ローカル）

Python 3.11、GCP の Application Default Credentials、Vertex AI / Sheets への権限が必要。

```powershell
python -m venv env_sej_pmo_bot
.\env_sej_pmo_bot\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item env.example .env
# .env を編集（トークンはチャットや Git に貼らない）
```

```powershell
$env:SLACK_BOT_TOKEN="..."
$env:SLACK_SIGNING_SECRET="..."
$env:SPREADSHEET_KEY="..."
$env:PROJECT_ID="test-yama-haj-2025"
$env:LOCATION="us-central1"
functions-framework --target=slack_bot_handler --port=8080
```

詳細は [documents/ローカルテスト手順.md](documents/ローカルテスト手順.md)。本番シートへ書くテストは、影響範囲を確認してから行う。

## デプロイ

`main` へ push すると GitHub Actions が `gcloud run deploy --source` する。環境変数・Secret・maxScale は現行リビジョンを継承する。コンソールへ Python を貼り付けない。

手順: [documents/GitHubデプロイ手順.md](documents/GitHubデプロイ手順.md)

CI が使えないときの手動デプロイ:

```bash
./cloud_run_files/deploy.sh
```

## ドキュメント

| ファイル | 用途 |
|---|---|
| [documents/利用マニュアル.md](documents/利用マニュアル.md) | Slack での使い方（CA 利用者・PMO） |
| [documents/GitHubデプロイ手順.md](documents/GitHubデプロイ手順.md) | 本番への出し方 |
| [documents/設計書.md](documents/設計書.md) | 処理フロー・シート列・環境変数 |
| [documents/要件定義書.md](documents/要件定義書.md) | 実装要件 |
| [documents/ローカルテスト手順.md](documents/ローカルテスト手順.md) | ローカル起動 |
| [documents/トラブルシューティング履歴.md](documents/トラブルシューティング履歴.md) | 過去の障害メモ |

## セキュリティ

- Slack 署名検証。リトライヘッダー付きは処理せず 200 を返す（二重登録防止）
- ランタイムの Slack トークンは Secret Manager
- デプロイ認証は Workload Identity Federation（キーファイルなし）。`main` 以外は GCP を偽装できない
- ログや Slack エラーにトークンを出さない

## 今後

- リポジトリの GitLab 移行（会社の SEJ 実装は GitLab が正）
- アカウント管理なのにメール抽出が空になる件の正規表現フォールバック
- `gemini-2.5-flash-lite` の後継評価（2026-10 前後退役見込み）
