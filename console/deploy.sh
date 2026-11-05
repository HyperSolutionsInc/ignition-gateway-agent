#!/usr/bin/env bash
# Build and deploy the fleet console stack.
#
#   AWS_PROFILE=<profile> console/deploy.sh 50.154.200.106/32[,<cidr>...] [HAMF_API_URL]
#
# Needs the AWS CLI and python3. The account's IoT side (tools/aws_setup.sh) should exist first.
set -euo pipefail
cd "$(dirname "$0")"
CIDRS=${1:?usage: deploy.sh <allowed-cidrs,comma-separated> [hamf-api-url]}
HAMF=${2:-}
REGION=${AWS_REGION:-us-east-2}
STACK=${STACK:-hyper-console}
ACCT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS=hyper-console-artifacts-$ACCT-$REGION

rm -rf build && mkdir -p build/gateway_agent build/web
cp backend/console_api.py backend/package_server.py build/
cp ../gateway_agent/__init__.py ../gateway_agent/hamf.py build/gateway_agent/
cp web/index.html web/app.js web/app.css build/web/

aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null || {
  if [ "$REGION" = us-east-1 ]; then aws s3api create-bucket --bucket "$ARTIFACTS" >/dev/null
  else aws s3api create-bucket --bucket "$ARTIFACTS" --region "$REGION" \
         --create-bucket-configuration LocationConstraint="$REGION" >/dev/null; fi
  aws s3api put-public-access-block --bucket "$ARTIFACTS" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}
aws cloudformation package --region "$REGION" --template-file template.yaml --s3-bucket "$ARTIFACTS" \
  --output-template-file build/packaged.yaml >/dev/null
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file build/packaged.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --no-fail-on-empty-changeset \
  --parameter-overrides "AllowedCidrs=$CIDRS" "HamfApiUrl=$HAMF"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleUrl'].OutputValue" --output text
