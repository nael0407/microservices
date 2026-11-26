#!/usr/bin/env bash
# Provisioning Track C (Lambda Function URL + AWS Lambda + Amazon S3) di AWS Learner Lab (us-east-1)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REGION="${AWS_REGION:-us-east-1}"
ADMIN_API_KEY="${ADMIN_API_KEY:-dev-admin-key}"
HMAC_SECRET="${HMAC_SECRET:-dev-hmac-secret}"
EVENT_OPEN="${EVENT_OPEN:-true}"
EVENT_QUOTA="${EVENT_QUOTA:-50}"
PAY_WINDOW_MINUTES="${PAY_WINDOW_MINUTES:-30}"

API_FN="${API_FN:-ticket-api-fn}"
AUDIT_FN="${AUDIT_FN:-ticket-audit-fn}"

ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text --region "$REGION")
LAB_ROLE_ARN=$(aws iam get-role --role-name LabRole --query 'Role.Arn' --output text --region "$REGION")
BUCKET_NAME="${BUCKET_NAME:-ticketing-serverless-${ACCOUNT_ID}}"

echo "==> Account : $ACCOUNT_ID | Region: $REGION"
echo "==> Role    : $LAB_ROLE_ARN"
echo "==> Bucket  : $BUCKET_NAME"

# 1. S3 Bucket (Private + Block Public Access + SSE-S3)
if ! aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Membuat S3 bucket: $BUCKET_NAME"
  aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null
fi

aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --region "$REGION" >/dev/null

aws s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
  --region "$REGION" >/dev/null

# 2. Package Lambda code (hanya stdlib + boto3 bawaan runtime)
mkdir -p "$ROOT_DIR/dist"
ZIP_PATH="$ROOT_DIR/dist/lambda_bundle.zip"
rm -f "$ZIP_PATH"
zip -qr "$ZIP_PATH" src -x "*/__pycache__/*" "*.pyc"
echo "==> Bundle dibuat: $ZIP_PATH"

deploy_lambda() {
  local fn_name="$1"
  local handler="$2"
  local env_json="$3"

  if aws lambda get-function --function-name "$fn_name" --region "$REGION" >/dev/null 2>&1; then
    echo "==> Updating Lambda: $fn_name"
    aws lambda update-function-code \
      --function-name "$fn_name" \
      --zip-file "fileb://$ZIP_PATH" \
      --region "$REGION" >/dev/null
    aws lambda wait function-updated-v2 --function-name "$fn_name" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$fn_name" \
      --runtime python3.12 \
      --handler "$handler" \
      --role "$LAB_ROLE_ARN" \
      --timeout 10 \
      --memory-size 256 \
      --environment "$env_json" \
      --region "$REGION" >/dev/null
  else
    echo "==> Creating Lambda: $fn_name"
    aws lambda create-function \
      --function-name "$fn_name" \
      --runtime python3.12 \
      --role "$LAB_ROLE_ARN" \
      --handler "$handler" \
      --zip-file "fileb://$ZIP_PATH" \
      --timeout 10 \
      --memory-size 256 \
      --environment "$env_json" \
      --region "$REGION" >/dev/null
  fi
  aws lambda wait function-active-v2 --function-name "$fn_name" --region "$REGION"
}

# 3. Deploy Audit Lambda + S3 Event Notification
deploy_lambda "$AUDIT_FN" "src.audit_handler.handler" "{\"Variables\":{\"BUCKET_NAME\":\"$BUCKET_NAME\"}}"
AUDIT_ARN=$(aws lambda get-function --function-name "$AUDIT_FN" --query 'Configuration.FunctionArn' --output text --region "$REGION")

aws lambda remove-permission \
  --function-name "$AUDIT_FN" \
  --statement-id "s3-invoke-audit" \
  --region "$REGION" >/dev/null 2>&1 || true

aws lambda add-permission \
  --function-name "$AUDIT_FN" \
  --statement-id "s3-invoke-audit" \
  --action "lambda:InvokeFunction" \
  --principal "s3.amazonaws.com" \
  --source-arn "arn:aws:s3:::${BUCKET_NAME}" \
  --source-account "$ACCOUNT_ID" \
  --region "$REGION" >/dev/null

aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET_NAME" \
  --notification-configuration "{
    \"LambdaFunctionConfigurations\": [
      {
        \"Id\": \"TicketCreatedAudit\",
        \"LambdaFunctionArn\": \"${AUDIT_ARN}\",
        \"Events\": [\"s3:ObjectCreated:*\"],
        \"Filter\": {
          \"Key\": {
            \"FilterRules\": [
              {\"Name\": \"prefix\", \"Value\": \"tickets/\"},
              {\"Name\": \"suffix\", \"Value\": \".json\"}
            ]
          }
        }
      }
    ]
  }" \
  --region "$REGION" >/dev/null
echo "==> S3 Event Notification terpasang ke $AUDIT_FN"

# 4. Deploy API Lambda + Function URL
API_ENV="{\"Variables\":{\"BUCKET_NAME\":\"$BUCKET_NAME\",\"EVENT_OPEN\":\"$EVENT_OPEN\",\"EVENT_QUOTA\":\"$EVENT_QUOTA\",\"PAY_WINDOW_MINUTES\":\"$PAY_WINDOW_MINUTES\",\"ADMIN_API_KEY\":\"$ADMIN_API_KEY\",\"HMAC_SECRET\":\"$HMAC_SECRET\"}}"
deploy_lambda "$API_FN" "src.api_handler.handler" "$API_ENV"

if aws lambda get-function-url-config --function-name "$API_FN" --region "$REGION" >/dev/null 2>&1; then
  FUNCTION_URL=$(aws lambda get-function-url-config --function-name "$API_FN" --query 'FunctionUrl' --output text --region "$REGION")
else
  FUNCTION_URL=$(aws lambda create-function-url-config --function-name "$API_FN" --auth-type NONE --query 'FunctionUrl' --output text --region "$REGION")
fi
FUNCTION_URL="${FUNCTION_URL%/}"

aws lambda remove-permission \
  --function-name "$API_FN" \
  --statement-id "public-function-url" \
  --region "$REGION" >/dev/null 2>&1 || true

aws lambda add-permission \
  --function-name "$API_FN" \
  --statement-id "public-function-url" \
  --action "lambda:InvokeFunctionUrl" \
  --principal "*" \
  --function-url-auth-type NONE \
  --region "$REGION" >/dev/null

aws lambda remove-permission \
  --function-name "$API_FN" \
  --statement-id "public-function-url-invoke" \
  --region "$REGION" >/dev/null 2>&1 || true

aws lambda add-permission \
  --function-name "$API_FN" \
  --statement-id "public-function-url-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "*" \
  --invoked-via-function-url \
  --region "$REGION" >/dev/null

cat <<EOF > "$ROOT_DIR/.env"
AWS_REGION=$REGION
BUCKET_NAME=$BUCKET_NAME
API_FN=$API_FN
AUDIT_FN=$AUDIT_FN
FUNCTION_URL=$FUNCTION_URL
ADMIN_API_KEY=$ADMIN_API_KEY
HMAC_SECRET=$HMAC_SECRET
EOF

echo ""
echo "=========================================="
echo "PROVISIONING TRACK C SELESAI"
echo "Function URL : $FUNCTION_URL"
echo "S3 Bucket    : $BUCKET_NAME"
echo "API Lambda   : $API_FN"
echo "Audit Lambda : $AUDIT_FN"
echo "=========================================="

