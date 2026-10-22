#!/bin/bash
# GitHub Actions の sej-pmo-bot-deploy SA が gcloud run deploy --source できるようにする。
# 要: gcloud auth login（オーナー）
# gcloud builds submit / _cloudbuild バケットは使わない。
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-test-yama-haj-2025}"
SA="sej-pmo-bot-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
SOURCE_BUCKET="gs://run-sources-${PROJECT_ID}-us-central1"
RUNTIME_SA="949120639074-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/run.admin" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.bucketViewer" \
  --condition=None

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/serviceusage.serviceUsageConsumer" \
  --condition=None

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${SA}" \
  --role="roles/iam.serviceAccountUser" \
  --project="$PROJECT_ID"

gcloud storage buckets add-iam-policy-binding "$SOURCE_BUCKET" \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.objectAdmin"

gcloud storage buckets add-iam-policy-binding "$SOURCE_BUCKET" \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.legacyBucketWriter"

echo "Granted. Re-run the GitHub Actions workflow Deploy Cloud Run."
