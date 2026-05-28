# Workshop Deployment Guide

この手順は、参加者が **GitHub public repository を Google Cloud Shell で clone** し、自分の新規 Google Cloud project にデモを deploy して、Cloud Run URL で動作確認するところまでを対象にします。

ローカル PC での実行は推奨しません。ワークショップ参加者は個人 Google アカウントで参加する想定のため、ローカル `gcloud` の会社アカウント、ADC quota project、Python version の混線を避けるためです。

> [!IMPORTANT]
> 本ワークショップでは実際に Google Cloud 上にアプリケーションを deploy し稼働させるため、Google Cloud の料金が発生します。
>
> 運営スタッフの実測では目安 **150 円前後** (2026-05-24 時点、為替・実行回数で変動)。

## Phase 0. 開場〜開始前にここまで進める

開場後に余裕がある人は、次の内容を進めておきましょう。これらステップを完了しておくことで、本編では Agent Runtime / Cloud Run の deploy と smoke test に集中できます。

- [Section 1. 事前準備](#1-事前準備)
- [Section 2. Repository を clone](#2-repository-を-clone)
- [Section 3. 環境変数を設定](#3-環境変数を設定)
- [Section 4. API と基本 IAM を準備](#4-api-と基本-iam-を準備)

## Phase 1. Workshop 本編の流れ

本編では、まず [Section 0 (全体構成)](#0-全体構成) を確認し、その後 Section 5–9 の bucket / IAM / Agent Runtime / Cloud Run deploy と smoke test に進みます。所要時間はおよそ **25〜35 分** を目安にしてください (内訳: Section 7 が 10〜15 分、Section 8 が 5〜10 分、その他で 10 分前後)。

Cloud Shell の tab を閉じた、または idle で session が切れた場合は、Section 3 の `export ...` と、Section 4 で追加した `PROJECT_NUMBER` / `CLOUD_RUN_SA` / `GCS_SIGNING_SERVICE_ACCOUNT` の export を再実行してから Section 5 以降に進んでください。

## 0. 全体構成

- Cloud Run: Web UI、ログイン、HTMX polling、Agent Runtime 呼び出し
- Agent Runtime: ADK workflow agent の実行、記事取得、要約、style 判断、画像生成
- Vertex AI / Gemini: text model と image model
- Cloud Storage: Agent Runtime が生成した画像 artifact の保存
- Signed URL: 非公開 bucket の画像をブラウザに表示

重要な設計判断:

- Agent Runtime の local filesystem は Cloud Run から見えないため、生成物は Cloud Storage に置く
- Cloud Run は UI と polling に集中し、workflow は Agent Runtime に寄せる
- Runtime から Cloud Run へ返す値は `agent/runtime_contract.py` の JSON contract で固定する
- Runtime 完了後に、workflow の進行状況と結果を JSON contract として Cloud Run に返す

## 1. 事前準備

Google Cloud Console で次を済ませます。

1. 新規 project を作成する
2. Billing account を project に紐づける
3. Console 上部の project selector で対象 project を選ぶ
4. Cloud Shell を開く

以後のコマンドは Cloud Shell で実行します。

## 2. Repository を clone

```bash
git clone https://github.com/kazumasa416/gemini-enterprise-agent-runtime-workshop.git
cd gemini-enterprise-agent-runtime-workshop
```

Cloud Shell では `git@github.com:...` の SSH clone は使いません。public repository を HTTPS URL で clone します。

Cloud Shell の Python が 3.10 以上であることを確認します。

```bash
python3 --version
```

> [!IMPORTANT]
> 3.10 未満の場合、この手順では進めないでください。Agent Runtime deploy で使う Agent Engine SDK / MCP が Python 3.10+ を要求します。

Cloud Shell では通常 `python3 -m venv` が使えます。Phase 0 の `preflight-cloud-shell.sh` は venv module の有無も確認します。

依存関係をインストールします。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt -c constraints-workshop.txt
```

`constraints-workshop.txt` はワークショップ用に固定した依存バージョンです。Cloud Shell、Cloud Run、Agent Runtime の build で同じ固定版を使います。

## 3. 環境変数を設定

`PROJECT_ID` は自分の project ID に置き換えます。`APP_PASSWORD` は自分用のログインパスワードに変更します。

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="asia-northeast1"
export AGENT_RUNTIME_LOCATION="us-central1"
export GOOGLE_CLOUD_LOCATION="global"

export SERVICE_NAME="graphic-recording-agent-demo"
export APP_PASSWORD="CHANGE_ME_TO_YOUR_PASSWORD"
export APP_SECRET_KEY="$(openssl rand -hex 32)"

export GEMINI_TEXT_MODEL="gemini-3.5-flash"
export GEMINI_IMAGE_MODEL="gemini-3-pro-image-preview"
export ARTICLE_FETCH_MAX_BYTES="2000000"

export GCS_BUCKET="${PROJECT_ID}-graphic-recording-artifacts"
export AGENT_RUNTIME_STAGING_BUCKET="${GCS_BUCKET}"
export GCS_ARTIFACT_PREFIX="artifacts"
export GCS_SIGNED_URL_TTL_SECONDS="28800"
```

Project と `gcloud` CLI の認証状態を確認します。

```bash
gcloud config set project "${PROJECT_ID}"
gcloud auth list
gcloud config get-value account
```

`gcloud config get-value account` が空、または意図した Google account ではない場合は、次を実行してから先に進みます。

```bash
gcloud auth login
gcloud config set account "YOUR_EMAIL"
```

続いて、Python SDK / Google client library が使う Application Default Credentials (ADC) を設定します。

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "${PROJECT_ID}"
gcloud auth application-default print-access-token >/dev/null && echo "ADC ok"
gcloud beta billing projects describe "${PROJECT_ID}"
```

> [!IMPORTANT]
> `billingEnabled: true` になっていない場合は、Console で billing を紐づけてから進んでください。

次に、ここまで実施した内容が正しく実行されているか、確認用のscript を実行しましょう。

```
./scripts/preflight-cloud-shell.sh
```

**`Preflight passed.`と表示されれば成功**です！
`preflight-cloud-shell.sh` が失敗した場合は、表示された `[NG]` を直してから再実行してください。preflight は Python version と `venv` module も確認します。

## 4. API と基本 IAM を準備

次の script を実行し、API の有効化と必要な IAM を設定します。

```bash
./scripts/bootstrap-gcp-project.sh
```

成功すると project number と Cloud Run runtime service account が表示されます。

API 有効化や初回の Vertex AI / Gemini 利用時に Console 上で追加確認が表示された場合は、その場で承認してから同じコマンドを再実行してください。

以降のワークショップで必要となる環境変数を設定しておきます。

```bash
export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
export GCS_SIGNING_SERVICE_ACCOUNT="${CLOUD_RUN_SA}"
```

## 5. Artifact bucket を作成

```bash
gcloud storage buckets create "gs://${GCS_BUCKET}" --location="${REGION}"
```

同じ bucket を次の 2 用途で使います。

- Agent Runtime deploy の staging bucket
- 生成画像 artifact の保存先

既に bucket が存在する場合は、`gcloud storage buckets create` は失敗します。その場合は bucket 名が自分の project 用であることを確認して先に進みます。

## 6. Runtime IAM を設定

次の script で、Agent Runtime と Cloud Run が Cloud Storage artifact を扱うための IAM を設定します。

```bash
./scripts/configure-runtime-iam.sh
```

この script は次を行います。

- Vertex AI service identity を作成または確認
- Cloud Run default service account に bucket 書き込み権限を付与
- `gcp-sa-aiplatform` と `gcp-sa-aiplatform-re` の Runtime 系 service agent に bucket 書き込み権限を付与
- Runtime 系 service agent に Cloud Run default service account で signed URL を作るための `roles/iam.serviceAccountTokenCreator` を付与

script の最後に `Export these values before deploy:` として `CLOUD_RUN_SA` / `GCS_SIGNING_SERVICE_ACCOUNT` の 2 行が表示されますが、いずれも **Section 4 で export 済みの値と同じ**です。表示された値が次と一致していることを確認できれば、追加の export は不要です。

- `CLOUD_RUN_SA` = `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`
- `GCS_SIGNING_SERVICE_ACCOUNT` = 同上 (`CLOUD_RUN_SA` と同じ値)

## 7. Agent Runtime を deploy

このコマンドは 10〜15 分かかることがあります。途中で出力が止まって見えても、Cloud Build / Agent Runtime の準備が進んでいる場合があります。エラーが出るまでは待ってください。
Agent Runtime の依存関係は、デフォルトで `constraints-workshop.txt` の固定版を使って deploy されます。

ここで詰まった場合は、[Section 10 (mock mode)](#10-困った場合-cloud-shell-mock-mode) に切り替えると UI 動作だけは確認できます。

```bash
export AGENT_DISPLAY_NAME="graphic-recording-agent"
export AGENT_RUNTIME_STAGING_BUCKET="${GCS_BUCKET}"
export GCS_SIGNING_SERVICE_ACCOUNT="${CLOUD_RUN_SA}"
export GOOGLE_CLOUD_LOCATION="global"

python scripts/deploy-agent-runtime.py
```

成功すると次のような出力になります。

```text
projects/887643395015/locations/us-central1/reasoningEngines/1234567890123456789
effective_identity=service-887643395015@gcp-sa-aiplatform-re.iam.gserviceaccount.com
```

1 行目に表示された `projects/.../locations/.../reasoningEngines/...` をそのまま `AGENT_RUNTIME_RESOURCE_NAME` に設定します。

> [!WARNING]
> 下の `...` や数字は例です。`PROJECT_NUMBER` や `RESOURCE_ID` という文字列をそのままコピーしないでください。実際に deploy 出力で表示された 1 行目をそのまま貼り付けます。

```bash
# 上の出力の 1 行目 (projects/.../reasoningEngines/...) を貼り付ける
export AGENT_RUNTIME_RESOURCE_NAME="PASTE_HERE"
```

2 行目に `effective_identity=...` が出力されている **場合のみ**、その identity を export して `configure-runtime-iam.sh` を再実行します。出力されていない場合は追加の IAM 設定は不要なので、そのまま Section 8 に進んでください。

```bash
# effective_identity= の後ろのメールアドレスを貼り付ける
export AGENT_RUNTIME_EFFECTIVE_IDENTITY="PASTE_HERE"
./scripts/configure-runtime-iam.sh
```

> [!NOTE]
> - Agent Runtime の deployment env では `GOOGLE_CLOUD_PROJECT` は予約名のため渡しません
> - Gemini model location は `GOOGLE_CLOUD_LOCATION=global` で固定します
> - `gemini-3.5-flash` が 404 の場合は Runtime logs を確認し、model ID または利用可能 region をスタッフに確認してください

## 8. Cloud Run を deploy

このコマンドは 5〜10 分かかることがあります。source upload、container build、revision 作成、traffic 切り替えが順番に実行されます。

ここで詰まった場合も、[Section 10 (mock mode)](#10-困った場合-cloud-shell-mock-mode) で UI を確認しながら進められます。

```bash
export MOCK_MODE="false"
export AGENT_BACKEND="runtime"
export AGENT_RUNTIME_LOCATION="us-central1"
export GOOGLE_CLOUD_LOCATION="global"
export GCS_SIGNING_SERVICE_ACCOUNT="${CLOUD_RUN_SA}"

./scripts/deploy-cloud-run.sh
```

成功すると Cloud Run URL が表示されます。

```text
Service URL: https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app
```

この URL を控えます。

> [!IMPORTANT]
> 再 deploy する際は、Phase 0 で生成した `APP_SECRET_KEY` の値をそのまま使い続けてください。値が変わると既存の login cookie が無効になり、再ログインが必要になります。Cloud Shell の tab を閉じた場合に備え、現在の値を `echo "${APP_SECRET_KEY}"` で控えておくと安全です。

## 9. ブラウザで smoke test

Cloud Run URL を開きます。

```text
https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app
```

ログインパスワードは `APP_PASSWORD` に設定した値です。

```text
CHANGE_ME_TO_YOUR_PASSWORD
```

最初に試す記事 URL は次のものを使ってください。サイト構造や bot 対策により本文抽出に失敗する URL があるため、まずは動作確認済みのもので smoke test を完了させてから自由な URL を試すことをおすすめします。

```text
https://zenn.dev/mbk_digital/articles/db99a38e334462
```

次を確認します。

1. ログインできる
2. 公開ブログ記事 URL を入力して `Agent に要約させる` を押す
3. 実行中に `Agent Runtime で処理中`、経過秒数、`現在の目安` が表示される
4. 要約確認画面が表示される
5. `グラレコを生成` を押す
6. 実行中に画像生成の待ち時間目安と `現在の目安` が表示される
7. グラレコ結果が表示される
8. `Agent style` と style 判断理由が表示される
9. 画像 artifact が表示され、`画像を保存` が使える
10. フィードバックを入力して再生成できる

画像生成は 1〜3 分かかることがあります。止まって見える場合でも、経過秒数が更新されていれば Cloud Run の polling は動いています。通常より長い場合は画面に注意メッセージが表示されます。

## 10. 困った場合: Cloud Shell mock mode

Billing、IAM、Agent Runtime deploy で進めない場合は、mock mode に切り替えると Cloud Shell の Web Preview でアプリの画面を確認できます。Agent Runtime / Cloud Run / Cloud Storage の deploy は行いません。

Cloud Shell ターミナルで起動します。

```bash
export MOCK_MODE="true"
export AGENT_BACKEND="local"
export APP_PASSWORD="mock"
export APP_SECRET_KEY="mock-secret-key-for-local-only"

python -m uvicorn web.main:app --host 0.0.0.0 --port 8080
```

起動したら、Cloud Shell 右上の **Web Preview** から **Preview on port 8080** を開きます。ログインパスワードは `mock` です。

この mode は画面確認用です。Agent Runtime、Cloud Run、Cloud Storage、signed URL は使いません。

## 11. ログ確認

運営スタッフに相談する場合は、まず診断 script の出力を共有してください。

```bash
./scripts/diagnose-deployment.sh
```

先頭の `SUMMARY` だけで、Cloud Run、Agent Runtime、bucket、直近エラーの大半を切り分けられます。必要な場合だけ `DETAILS` 以降を確認します。

Cloud Run logs:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="'"${SERVICE_NAME}"'"' \
  --project="${PROJECT_ID}" \
  --limit=100 \
  --format='value(timestamp,severity,textPayload,jsonPayload.message,jsonPayload.error)'
```

Agent Runtime logs:

```bash
RUNTIME_ID="$(echo "${AGENT_RUNTIME_RESOURCE_NAME}" | awk -F/ '{print $NF}')"
gcloud logging read 'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND resource.labels.reasoning_engine_id="'"${RUNTIME_ID}"'"' \
  --project="${PROJECT_ID}" \
  --limit=100 \
  --format='value(timestamp,textPayload)'
```

Staging / artifact bucket:

```bash
gcloud storage ls -r "gs://${GCS_BUCKET}/agent_engine/**"
gcloud storage ls -r "gs://${GCS_BUCKET}/${GCS_ARTIFACT_PREFIX}/**" | tail
```

## 12. よくあるエラー

各 script (`bootstrap-gcp-project.sh` / `configure-runtime-iam.sh` / `deploy-agent-runtime.py` / `deploy-cloud-run.sh`) は基本的にべき等です。失敗した場合は、まずエラーメッセージを確認のうえ、同じコマンドを再実行してみてください。それでも解消しない場合は、次の代表的なエラーパターンと対処を参照します。

### Billing が無効

症状:

```text
Billing account for project ... is not found
UREQ_PROJECT_BILLING_NOT_FOUND
```

対処: Console で billing account を project に紐づけてください。

### Cloud Run source deploy で 403 (`storage.objects.get` denied)

症状:

```text
ERROR: (gcloud.run.deploy) INVALID_ARGUMENT: Invalid build request.
could not resolve source: googleapi: Error 403:
<PROJECT_NUMBER>-compute@developer.gserviceaccount.com does not have storage.objects.get access
to the Google Cloud Storage object.
```

対処: 2024 年 4 月以降に作られた project では、`gcloud run deploy --source .` の Cloud Build worker が Compute Engine default SA に切り替わっており、source bucket / Artifact Registry / Cloud Logging への権限が初期状態では付いていません。`./scripts/bootstrap-gcp-project.sh` を最新版で再実行すれば自動で付与されます。既に deploy 中で詰まった既存 project は、次のコマンドだけでも復旧できます。

```bash
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role "roles/cloudbuild.builds.builder"
```

### Python 3.9 で deploy している

症状:

```text
MCP requires Python 3.10 or above
module 'google.genai.types' has no attribute ...
```

対処: Cloud Shell で `python3 --version` を確認し、3.10+ の venv を作り直します。

### staging_bucket がない

症状:

```text
Please provide a `staging_bucket`
```

対処: 環境変数の `GCS_BUCKET` と `AGENT_RUNTIME_STAGING_BUCKET` を export してから `python scripts/deploy-agent-runtime.py` を実行します。

### `GOOGLE_CLOUD_PROJECT` が reserved

症状:

```text
Environment variable name 'GOOGLE_CLOUD_PROJECT' is reserved
```

対処: Agent Runtime deployment env に `GOOGLE_CLOUD_PROJECT` を渡してはいけません。最新の `scripts/deploy-agent-runtime.py` を使ってください。

### `gemini-3.5-flash` が 404

症状:

```text
Publisher Model ... locations/us-central1 ... gemini-3.5-flash was not found
```

対処: Agent Runtime に `GOOGLE_CLOUD_LOCATION=global` が渡っているか確認し、Runtime を再 deploy します。

### 画面のエラーが空

対処: 最新の Cloud Run revision に再 deploy してください。現在の実装では例外本文が空でも型名を表示し、Cloud Logging に stack trace を出します。

### signed URL が 403

対処: `./scripts/configure-runtime-iam.sh` を再実行し、Cloud Run service account を signer にして Runtime effective identity に `roles/iam.serviceAccountTokenCreator` が付いていることを確認します。

## 13. コスト確認

正確な発生額は、この repository や `gcloud` の通常コマンドだけでは分かりません。Cloud Billing Console で project filter をかけて確認します。

1. Google Cloud Console で Billing を開く
2. Reports を開く
3. Filter で `PROJECT_ID` を選ぶ
4. 今日の日付範囲にする
5. Service ごとの cost を確認する

今回の構成で課金対象になり得るもの:

- Cloud Build
- Artifact Registry
- Cloud Run
- Vertex AI / Agent Runtime
- Gemini text model
- Gemini image model
- Cloud Storage
- Cloud Logging

> [!WARNING]
> 短時間の検証でも画像生成と Agent Runtime は課金対象になり得ます。ワークショップ後は必ず後片付け (Section 14) を実施してください。

## 14. 後片付け

まず script で workshop リソースを削除します。`--yes` を付けない場合は、確認 prompt で `delete` と入力しない限り削除されません。

```bash
./scripts/cleanup-gcp-resources.sh
```

自動実行やリハーサルで confirmation を省略したい場合だけ、明示的に `--yes` を付けます。

```bash
./scripts/cleanup-gcp-resources.sh --yes
```

削除前に状態を見たい場合:

```bash
./scripts/diagnose-deployment.sh
```

この cleanup script は Google Cloud project 自体は削除しません。

Disposable project の場合は、project ごと削除するのが最も確実です。

> [!CAUTION]
> これは **ワークショップ専用に作った disposable project 限定** の手順です。個人利用中の project や会社 project では絶対に実行しないでください。削除前に Console の project selector と `PROJECT_ID` を必ず確認してください。

```bash
gcloud projects describe "${PROJECT_ID}"
```

問題なければ project を削除します。

> [!CAUTION]
> 削除すると project 内のリソースは復元できません。実行前に `PROJECT_ID` がワークショップ用 disposable project のものであることを再確認してください。

```bash
gcloud projects delete "${PROJECT_ID}"
```

ワークショップお疲れ様でした！
