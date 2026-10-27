# デプロイ手順

リポジトリ概要は [README](../README.md)。使い方は [利用マニュアル](利用マニュアル.md)。

正本は GitLab の `main`。本番 Cloud Run はコンソール貼り付けではなく、`main` への push で GitLab CI が更新する。このファイルはデプロイ手順のみ。

## 本番構成（固定）

| 項目 | 値 |
|---|---|
| GCP プロジェクト | `test-yama-haj-2025` |
| サービス名 | `slack-bot-handler-us` |
| リージョン | `us-central1` |
| URL | https://slack-bot-handler-us-949120639074.us-central1.run.app |
| 関数エントリ | `slack_bot_handler` |
| Vertex AI `LOCATION` | `global` |
| Vertex AI モデル | `gemini-3.5-flash-lite`（環境変数 `GEMINI_MODEL`） |
| Slack トークン | Secret Manager（`BOT_SEJ_PMO_SLACK_BOT_TOKEN` / `BOT_SEJ_PMO_SLACK_SIGNING_SECRET`） |

東京リージョンやサービス名 `slack-bot-handler`（接尾辞なし）へはデプロイしない。

## 通常の更新（CI/CD）

1. 変更を `main` にマージして push する（GitLab リモート）。
2. GitLab CI `deploy_cloud_run` ジョブが `gcloud run deploy --source` で本番を自動更新する（env / Secret は上書きしない）。
3. 新リビジョンが Ready になることを確認する。`LOCATION=global` と Secret 参照が残っていること。

コンソールで Python を貼り付けてビルドする運用はしない。

認証は GitLab → Workload Identity Federation（サービスアカウント `sej-pmo-bot-deploy@test-yama-haj-2025.iam.gserviceaccount.com`）。キーファイルは使わない。`main` 以外のブランチからは GCP を偽装できない。

### GitLab CI（本番稼働）

パイプライン設定は `.gitlab-ci.yml`。WIF（pool `gitlab-pool` / provider `gitlab-provider`）設定済み。初回成功は 2026-10-27（リビジョン `slack-bot-handler-us-00024-hc8`）。

### GitHub Actions（アーカイブ）

旧 GitHub Actions ワークフロー（`.github/workflows/deploy.yml`）は二重デプロイ防止のため `workflow_dispatch`（手動実行のみ）に切り替え済み。普段の push では動作しない。

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
