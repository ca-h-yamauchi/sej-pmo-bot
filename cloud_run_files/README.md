# Cloud Run 設定

本番サービスは `slack-bot-handler-us`（`us-central1`）。正本は GitHub `main`、通常のデプロイは Cloud Build。

## ファイル構成

- `cloud_run.yaml` - エクスポートした現行設定（機密を含む。Git 対象外）
- `cloud_run.yaml.template` - Secret Manager 参照つきのテンプレート
- `deploy.sh` - リポジトリルートからソースだけを手動デプロイするスクリプト
- `README.md` - このファイル

## 通常のデプロイ

`main` へ push する。手順の正本は [`documents/GitHubデプロイ手順.md`](../documents/GitHubデプロイ手順.md)。

## 手動デプロイ

```bash
./cloud_run_files/deploy.sh
```

`--set-env-vars` でトークンを平文上書きしない。maxScale も変えない。

## 現行設定のエクスポート

```bash
gcloud run services describe slack-bot-handler-us \
  --project test-yama-haj-2025 \
  --region us-central1 \
  --format export > cloud_run_files/cloud_run.yaml
```

## 本番で維持する設定

- `LOCATION=us-central1`
- Slack トークンは Secret Manager
- `maxScale: 1`
- Gemini 用に `asia-northeast1` へは出さない
