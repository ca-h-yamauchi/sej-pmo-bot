#!/bin/bash
# Cloud Run YAMLファイルを使ったデプロイスクリプト

set -e

# 設定
PROJECT_ID="test-yama-haj-2025"
REGION="asia-northeast1"
SERVICE_NAME="slack-bot-handler"

# 環境変数の確認
if [ -z "$SLACK_BOT_TOKEN" ] || [ -z "$SLACK_SIGNING_SECRET" ] || [ -z "$SPREADSHEET_KEY" ]; then
    echo "エラー: 環境変数が設定されていません"
    echo "以下の環境変数を設定してください:"
    echo "  - SLACK_BOT_TOKEN"
    echo "  - SLACK_SIGNING_SECRET"
    echo "  - SPREADSHEET_KEY"
    exit 1
fi

# プロジェクト番号を取得
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

# YAMLファイルを生成（テンプレートから）
sed -e "s/YOUR_PROJECT_ID/$PROJECT_ID/g" \
    -e "s/YOUR_PROJECT_NUMBER/$PROJECT_NUMBER/g" \
    -e "s|YOUR_SLACK_BOT_TOKEN|$SLACK_BOT_TOKEN|g" \
    -e "s|YOUR_SLACK_SIGNING_SECRET|$SLACK_SIGNING_SECRET|g" \
    -e "s|YOUR_SPREADSHEET_KEY|$SPREADSHEET_KEY|g" \
    cloud_run.yaml.template > cloud_run.yaml

echo "YAMLファイルを生成しました: cloud_run.yaml"

# ソースコードをデプロイ（YAMLファイルは参照用）
echo "Cloud Runにデプロイしています..."
gcloud run deploy $SERVICE_NAME \
  --source . \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN",SLACK_SIGNING_SECRET="$SLACK_SIGNING_SECRET",SPREADSHEET_KEY="$SPREADSHEET_KEY",PROJECT_ID="$PROJECT_ID",LOCATION="$REGION" \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 100 \
  --min-instances 0

echo "デプロイが完了しました！"
