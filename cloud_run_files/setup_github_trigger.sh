#!/bin/bash
# GitHub 接続が COMPLETE になったあと、main トリガーを作る。
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-test-yama-haj-2025}"
REGION="us-central1"
CONNECTION="sej-pmo-bot-github"
REPO_NAME="sej-pmo-bot"
REMOTE_URI="https://github.com/ca-h-yamauchi/sej-pmo-bot.git"
TRIGGER_NAME="sej-pmo-bot-main"

stage="$(gcloud builds connections describe "$CONNECTION" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(installationState.stage)')"

echo "Connection stage: ${stage}"
if [[ "$stage" != "COMPLETE" ]]; then
  echo "GitHub 承認がまだ完了していません。"
  echo "Console: https://console.cloud.google.com/cloud-build/connections?project=${PROJECT_ID}"
  gcloud builds connections describe "$CONNECTION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(installationState.actionUri)'
  exit 1
fi

if ! gcloud builds repositories describe "$REPO_NAME" \
  --connection="$CONNECTION" \
  --region="$REGION" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud builds repositories create "$REPO_NAME" \
    --remote-uri="$REMOTE_URI" \
    --connection="$CONNECTION" \
    --region="$REGION" \
    --project="$PROJECT_ID"
fi

if ! gcloud builds triggers describe "$TRIGGER_NAME" \
  --region="$REGION" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud builds triggers create github \
    --name="$TRIGGER_NAME" \
    --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION}/repositories/${REPO_NAME}" \
    --branch-pattern="^main$" \
    --build-config=cloudbuild.yaml \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --description="Deploy sej-pmo-bot main to slack-bot-handler-us"
fi

echo "Trigger ${TRIGGER_NAME} is ready. Future pushes to main will deploy."
