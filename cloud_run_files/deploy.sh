#!/bin/bash
# リポジトリルートのソースを本番 Cloud Run へ手動デプロイする。
# 環境変数・Secret・maxScale は既存リビジョンを継承する。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT_ID="${PROJECT_ID:-test-yama-haj-2025}"
REGION="us-central1"
SERVICE_NAME="slack-bot-handler-us"
BASE_IMAGE="us-central1-docker.pkg.dev/serverless-runtimes/google-22/runtimes/python311"

echo "Deploying ${SERVICE_NAME} to ${REGION} from ${ROOT}"
echo "Env vars and secrets will not be overwritten."

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --function slack_bot_handler \
  --base-image "$BASE_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT_ID"

echo "Deploy finished. Confirm LOCATION=us-central1 and Secret Manager refs are intact."
