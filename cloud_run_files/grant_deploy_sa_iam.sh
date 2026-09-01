#!/bin/bash
# GitHub Actions の sej-pmo-bot-deploy SA が gcloud builds submit できるようにする。
# 要: gcloud auth login（オーナー）
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-test-yama-haj-2025}"
SA="sej-pmo-bot-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="gs://${PROJECT_ID}_cloudbuild"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/serviceusage.serviceUsageConsumer" \
  --condition=None

gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.legacyBucketWriter"

echo "Granted. Re-run the failed GitHub Actions workflow."
