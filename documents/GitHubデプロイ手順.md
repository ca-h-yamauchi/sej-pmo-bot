# デプロイ手順

正本は GitHub の `main`。本番 Cloud Run はコンソール貼り付けではなく、`main` への push で更新する。

## 本番構成（固定）

| 項目 | 値 |
|---|---|
| GCP プロジェクト | `test-yama-haj-2025` |
| サービス名 | `slack-bot-handler-us` |
| リージョン | `us-central1` |
| URL | https://slack-bot-handler-us-949120639074.us-central1.run.app |
| 関数エントリ | `slack_bot_handler` |
| Vertex AI `LOCATION` | `us-central1`（`gemini-2.5-flash-lite` は `asia-northeast1` では使えない） |
| Slack トークン | Secret Manager（`BOT_SEJ_PMO_SLACK_BOT_TOKEN` / `BOT_SEJ_PMO_SLACK_SIGNING_SECRET`） |

東京リージョンやサービス名 `slack-bot-handler`（接尾辞なし）へはデプロイしない。

## 通常の更新（CI/CD）

1. 変更を `main` にマージして push する。
2. Cloud Build トリガー `sej-pmo-bot-main` が [`cloudbuild.yaml`](../cloudbuild.yaml) を実行する。
3. 新リビジョンが Ready になることを確認する。`LOCATION=us-central1` と Secret 参照が残っていること。

コンソールで Python を貼り付けてビルドする運用はしない。

## 手動デプロイ（フォールバック）

CI が使えないときだけ、リポジトリルートから:

```bash
# Linux / Git Bash
./cloud_run_files/deploy.sh
```

または:

```bash
gcloud run deploy slack-bot-handler-us \
  --source . \
  --function slack_bot_handler \
  --base-image us-central1-docker.pkg.dev/serverless-runtimes/google-22/runtimes/python311 \
  --region us-central1 \
  --project test-yama-haj-2025
```

環境変数・Secret・maxScale はコマンドで渡さない（現行リビジョンを継承する）。

## リポジトリ

- URL: https://github.com/ca-h-yamauchi/sej-pmo-bot.git
- `.env` と `cloud_run_files/cloud_run.yaml` は Git に含めない
- 仮想環境 `env_sej_pmo_bot/` は `.gcloudignore` でアップロード対象外
