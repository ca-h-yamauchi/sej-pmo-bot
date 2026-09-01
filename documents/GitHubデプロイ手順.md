# デプロイ手順

正本は GitHub の `main`。本番 Cloud Run はコンソール貼り付けではなく、`main` への push で更新する。

**続き作業（GitHub Actions 設置・GitLab 移行・メール抽出・モデル更新）**は個人OSの [`docs/pmo/sej-pmo-bot-cicd-handoff.md`](../../work2026/docs/pmo/sej-pmo-bot-cicd-handoff.md) を正本にする。このファイルはデプロイ手順のみ。

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
2. GitHub Actions `Deploy Cloud Run` が `gcloud run deploy --source` で本番を更新する（env / Secret は上書きしない）。
3. 新リビジョンが Ready になることを確認する。`LOCATION=us-central1` と Secret 参照が残っていること。

コンソールで Python を貼り付けてビルドする運用はしない。

認証は GitHub → Workload Identity Federation（サービスアカウント `sej-pmo-bot-deploy@test-yama-haj-2025.iam.gserviceaccount.com`）。キーファイルは使わない。`main` 以外のブランチからは GCP を偽装できない。

### 初回だけ: GitHub Actions ワークフローを置く

ローカル PAT には `workflow` スコープがないため、ワークフローファイルは GitHub の Web から追加する。

1. https://github.com/ca-h-yamauchi/sej-pmo-bot/new/main?filename=.github/workflows/deploy.yml を開く
2. [`cloud_run_files/github-deploy.yml`](../cloud_run_files/github-deploy.yml) の内容を貼る
3. `main` にコミットする（このコミットで初回の自動デプロイが走る）

WIF（pool `github-pool` / provider `github-provider`）は設定済み。

### 任意: Cloud Build ネイティブ GitHub 接続

接続 `sej-pmo-bot-github`（`us-central1`）は PENDING_USER_OAUTH。承認すれば Cloud Build トリガーでも同じ YAML を回せる。

1. [Cloud Build 接続](https://console.cloud.google.com/cloud-build/connections?project=test-yama-haj-2025) を開く
2. `sej-pmo-bot-github` の案内リンクで GitHub を許可し、リポジトリ `ca-h-yamauchi/sej-pmo-bot` を選択する
3. `./cloud_run_files/setup_github_trigger.sh` を実行する

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
