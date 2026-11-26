#!/usr/bin/env bash
# Menghapus seluruh resource Track C (2 Lambda, S3 Bucket, dan artefak lokal)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

REGION="${AWS_REGION:-us-east-1}"
API_FN="${API_FN:-ticket-api-fn}"
AUDIT_FN="${AUDIT_FN:-ticket-audit-fn}"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text --region "$REGION")
BUCKET_NAME="${BUCKET_NAME:-ticketing-serverless-${ACCOUNT_ID}}"

echo "==> Menghapus Lambda Function URL & functions..."
aws lambda delete-function-url-config --function-name "$API_FN" --region "$REGION" >/dev/null 2>&1 || true
aws lambda delete-function --function-name "$API_FN" --region "$REGION" >/dev/null 2>&1 || true
aws lambda delete-function --function-name "$AUDIT_FN" --region "$REGION" >/dev/null 2>&1 || true

if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Mengosongkan & menghapus S3 bucket: $BUCKET_NAME"
  aws s3 rm "s3://${BUCKET_NAME}" --recursive --region "$REGION" >/dev/null || true
  aws s3api delete-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null
fi

rm -rf "$ROOT_DIR/dist" "$ROOT_DIR/.env"
echo "==> Cleanup selesai."

