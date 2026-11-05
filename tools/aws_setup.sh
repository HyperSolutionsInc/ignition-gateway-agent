#!/usr/bin/env bash
# Set up an AWS account for hands-free gateways (docs/aws-fleet-provisioning.md), in one go:
#   gateway policy, S3 bucket, IAM roles, telemetry rule, provisioning template, claim certificate.
# Re-running skips what already exists. The claim certificate and key are written to
# ./claim/ (mode 0600); copy them into docker/claim/ before building the image.
#
#   AWS_PROFILE=<profile> tools/aws_setup.sh
#
# The telemetry rule written here archives each message to the bucket (telemetry/<thing>/...),
# which is what the end-to-end test used. Point it at the real consumer for production.
set -euo pipefail
cd "$(dirname "$0")/.."
REGION=${AWS_REGION:-us-east-2}
P="--region $REGION"
ACCT=$(aws sts get-caller-identity $P --query Account --output text)
BUCKET=${BUCKET:-hyper-gateway-$ACCT}
TEMPLATE=hyper-gateway-fleet
OUT=${OUT:-claim}
echo "account $ACCT, region $REGION, bucket $BUCKET"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

extract() {  # the JSON block under a heading of docs/aws-fleet-provisioning.md
  python3 - "$1" <<'PY'
import json, re, sys
text = open("docs/aws-fleet-provisioning.md").read()
block = re.search(r"## %s.*?```json\n(.*?)```" % re.escape(sys.argv[1]), text, re.S).group(1)
json.loads(block)
print(block)
PY
}

echo "== gateway policy"
aws iot get-policy $P --policy-name hyper-gateway >/dev/null 2>&1 ||
  aws iot create-policy $P --policy-name hyper-gateway --policy-document file://docs/aws-iot-policy.json --query policyArn --output text

echo "== bucket"
aws s3api head-bucket $P --bucket "$BUCKET" 2>/dev/null || {
  if [ "$REGION" = us-east-1 ]; then aws s3api create-bucket $P --bucket "$BUCKET" >/dev/null
  else aws s3api create-bucket $P --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null; fi
}
aws s3api put-public-access-block $P --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "== IAM roles"
cat > "$tmp/trust.json" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"iot.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam get-role --role-name hyper-gateway-provisioning >/dev/null 2>&1 || {
  aws iam create-role --role-name hyper-gateway-provisioning --assume-role-policy-document "file://$tmp/trust.json" >/dev/null
  aws iam attach-role-policy --role-name hyper-gateway-provisioning \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSIoTThingsRegistration
  NEW_ROLE=1
}
aws iam get-role --role-name hyper-gateway-telemetry-s3 >/dev/null 2>&1 || {
  aws iam create-role --role-name hyper-gateway-telemetry-s3 --assume-role-policy-document "file://$tmp/trust.json" >/dev/null
  NEW_ROLE=1
}
aws iam put-role-policy --role-name hyper-gateway-telemetry-s3 --policy-name put-telemetry --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/telemetry/*\"}]}"
[ -n "${NEW_ROLE:-}" ] && { echo "waiting for IAM"; sleep 15; }

echo "== telemetry rule"
cat > "$tmp/rule.json" <<EOF
{"sql":"SELECT *, topic(2) AS gateway_id FROM 'hyper/+/telemetry'","awsIotSqlVersion":"2016-03-23","ruleDisabled":false,
 "actions":[{"s3":{"roleArn":"arn:aws:iam::$ACCT:role/hyper-gateway-telemetry-s3","bucketName":"$BUCKET",
   "key":"telemetry/\${topic(2)}/\${timestamp()}-\${newuuid()}.json"}}]}
EOF
aws iot get-topic-rule $P --rule-name hyper_telemetry >/dev/null 2>&1 ||
  aws iot create-topic-rule $P --rule-name hyper_telemetry --topic-rule-payload "file://$tmp/rule.json"

echo "== provisioning template"
extract "3. Provisioning template" > "$tmp/template.json"
aws iot describe-provisioning-template $P --template-name $TEMPLATE >/dev/null 2>&1 ||
  aws iot create-provisioning-template $P --template-name $TEMPLATE --enabled \
    --provisioning-role-arn "arn:aws:iam::$ACCT:role/hyper-gateway-provisioning" \
    --template-body "file://$tmp/template.json" --query templateArn --output text

echo "== claim certificate"
extract "4. Claim certificate" > "$tmp/claim-policy.json"
aws iot get-policy $P --policy-name hyper-gateway-claim >/dev/null 2>&1 ||
  aws iot create-policy $P --policy-name hyper-gateway-claim --policy-document "file://$tmp/claim-policy.json" >/dev/null
if [ ! -s "$OUT/claim.cert.pem" ]; then
  mkdir -p "$OUT"; chmod 700 "$OUT"
  ARN=$(aws iot create-keys-and-certificate $P --set-as-active \
        --certificate-pem-outfile "$OUT/claim.cert.pem" --private-key-outfile "$OUT/claim.private.key" \
        --query certificateArn --output text)
  chmod 600 "$OUT"/claim.*
  aws iot attach-policy $P --policy-name hyper-gateway-claim --target "$ARN"
  echo "claim certificate $ARN"
fi

echo "== done. Build with:"
echo "  docker build --build-arg IOT_ENDPOINT=$(aws iot describe-endpoint $P --endpoint-type iot:Data-ATS --query endpointAddress --output text) -t hyper-gateway:0.2.1 ."
