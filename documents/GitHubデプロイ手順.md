# GitHubデプロイ手順

## 1. GitHubリポジトリの作成

1. GitHubにログインし、新しいリポジトリを作成します
   - リポジトリ名: `sej-pmo-bot`（任意の名前で可）
   - 公開/非公開: お好みで設定
   - README、.gitignore、ライセンスは追加しない（既にローカルに存在するため）

## 2. リモートリポジトリの追加とプッシュ

以下のコマンドを実行して、GitHubリポジトリにプッシュします：

```bash
# リモートリポジトリを追加（YOUR_USERNAMEを実際のGitHubユーザー名に置き換えてください）
git remote add origin https://github.com/YOUR_USERNAME/sej-pmo-bot.git

# またはSSHを使用する場合
git remote add origin git@github.com:YOUR_USERNAME/sej-pmo-bot.git

# ブランチ名をmainに変更（GitHubのデフォルトに合わせる）
git branch -M main

# GitHubにプッシュ
git push -u origin main
```

## 3. 今後の更新手順

コードを変更した後は、以下のコマンドでGitHubに反映できます：

```bash
# 変更をステージング
git add .

# コミット
git commit -m "変更内容の説明"

# GitHubにプッシュ
git push
```

## 注意事項

- `.env`ファイルや機密情報は`.gitignore`で除外されています
- `env_sej_pmo_bot/`仮想環境ディレクトリも除外されています
- `cloud_run_files/cloud_run.yaml`（機密情報を含む）も除外されています
