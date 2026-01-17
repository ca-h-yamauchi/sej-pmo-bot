# Cloud Run YAMLファイル管理

このディレクトリには、Cloud Runサービスの設定を管理するためのファイルが含まれています。

## ファイル構成

- `cloud_run.yaml` - 現在のCloud Runサービスの設定（機密情報を含む）
- `cloud_run.yaml.template` - テンプレートファイル（機密情報をプレースホルダーに置き換え）
- `deploy.sh` - デプロイスクリプト
- `README.md` - このファイル

## 使用方法

### 1. YAMLファイルから設定を確認

現在の設定を確認する場合:
```bash
cat cloud_run_files/cloud_run.yaml
```

### 2. テンプレートからYAMLファイルを生成

機密情報を環境変数から設定してYAMLファイルを生成:
```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_SIGNING_SECRET="..."
export SPREADSHEET_KEY="..."
export PROJECT_ID="test-yama-haj-2025"

cd cloud_run_files
./deploy.sh
```

### 3. YAMLファイルを使ってデプロイ

**注意**: Cloud RunはYAMLファイルを直接デプロイすることはできませんが、設定を確認・管理するために使用できます。

実際のデプロイは以下のいずれかの方法を使用してください:

#### 方法A: gcloudコマンドでデプロイ（推奨）

```bash
gcloud run deploy slack-bot-handler \
  --source . \
  --region asia-northeast1 \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars SLACK_BOT_TOKEN="xoxb-...",SLACK_SIGNING_SECRET="...",SPREADSHEET_KEY="...",PROJECT_ID="test-yama-haj-2025",LOCATION="asia-northeast1" \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 100 \
  --min-instances 0
```

#### 方法B: GCP Consoleからデプロイ

1. GCP Console → Cloud Run → サービスを選択
2. 「新しいリビジョンの編集とデプロイ」をクリック
3. 設定を変更して「保存して再デプロイ」

### 4. YAMLファイルから設定をエクスポート

現在の設定をYAMLファイルとしてエクスポート:
```bash
gcloud run services describe slack-bot-handler \
  --region asia-northeast1 \
  --format export > cloud_run_files/cloud_run.yaml
```

## 設定項目の説明

### 重要な設定

- **containerConcurrency**: 80 - 1インスタンスあたりの同時リクエスト数
- **timeoutSeconds**: 300 - リクエストタイムアウト（5分）
- **memory**: 512Mi - メモリ制限
- **cpu**: 1000m (1 vCPU) - CPU制限
- **maxScale**: 100 - 最大インスタンス数
- **ingress**: all - インターネットからのアクセスを許可

### 環境変数

- `SLACK_BOT_TOKEN` - Slack Bot User OAuth Token
- `SLACK_SIGNING_SECRET` - Slack Signing Secret
- `SPREADSHEET_KEY` - Googleスプレッドシートのキー
- `PROJECT_ID` - GCPプロジェクトID
- `LOCATION` - Vertex AIのリージョン（asia-northeast1）

## セキュリティ注意事項

⚠️ **重要**: `cloud_run.yaml` には機密情報（トークン、シークレット）が含まれています。

- このファイルをGitにコミットしないでください
- `.gitignore` に追加することを推奨します
- テンプレートファイル（`cloud_run.yaml.template`）を使用して管理してください

## トラブルシューティング

### YAMLファイルの構文エラー

```bash
# YAMLファイルの構文を確認
yamllint cloud_run_files/cloud_run.yaml
```

### デプロイエラー

- 環境変数が正しく設定されているか確認
- プロジェクトIDとリージョンが正しいか確認
- Cloud Run APIが有効化されているか確認
