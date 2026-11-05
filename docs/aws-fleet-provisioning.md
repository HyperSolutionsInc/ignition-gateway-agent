# AWS setup for hands-free gateways

A new gateway gets its identity from AWS IoT **fleet provisioning by claim**, and its IMSs
from Device Shadows. Everything below is set up once per AWS account (region `us-east-2`),
not once per gateway.

[`tools/aws_setup.sh`](../tools/aws_setup.sh) does sections 1–4 in one run and writes the claim
certificate to `./claim/`. Its telemetry rule archives to S3 (as in the end-to-end test,
docs/TEST-PLAN.md); point the rule at the consumer for production.

## 1. Gateway policy

Create the IoT policy `hyper-gateway` from [`aws-iot-policy.json`](aws-iot-policy.json). It
limits each gateway's certificate to its own thing name, topics and shadows.

## 2. Telemetry rule

Create the rule `hyper_telemetry`:

```sql
SELECT *, topic(2) AS gateway_id FROM 'hyper/+/telemetry'
```

Give it an action that hands each message to the consumer that writes `ts.telemetry`
(Lambda or Kinesis). Telemetry sent before the rule exists is dropped.

## 3. Provisioning template

Create an IAM role that AWS IoT can assume, with the managed policy
`AWSIoTThingsRegistration`. Then create the template `hyper-gateway-fleet` with that role:

```json
{
  "Parameters": {
    "SerialNumber": {"Type": "String"},
    "AWS::IoT::Certificate::Id": {"Type": "String"}
  },
  "Resources": {
    "thing": {
      "Type": "AWS::IoT::Thing",
      "Properties": {
        "ThingName": {"Fn::Join": ["", ["hyper-gw-", {"Ref": "SerialNumber"}]]},
        "AttributePayload": {"serial": {"Ref": "SerialNumber"}}
      }
    },
    "certificate": {
      "Type": "AWS::IoT::Certificate",
      "Properties": {"CertificateId": {"Ref": "AWS::IoT::Certificate::Id"}, "Status": "ACTIVE"}
    },
    "policy": {
      "Type": "AWS::IoT::Policy",
      "Properties": {"PolicyName": "hyper-gateway"}
    }
  }
}
```

A gateway's thing is `hyper-gw-<serial>`. The serial is `GATEWAY_SERIAL` if the container
sets it (an asset tag, say), otherwise one the gateway generates on first boot and keeps. To
accept only known serials, add a pre-provisioning hook (a Lambda) to the template.

Extra template parameters can be sent with `IOT_PROVISIONING_PARAMETERS` (a JSON object of
strings), for example `{"Site": "Petersburg"}`.

## 4. Claim certificate

Create one certificate for the fleet, attach this policy to it, and put the certificate and
key in `docker/claim/` as `claim.cert.pem` and `claim.private.key` before building the image:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "iot:Connect",
     "Resource": "arn:aws:iot:us-east-2:*:client/provision-*"},
    {"Effect": "Allow", "Action": ["iot:Publish", "iot:Receive"],
     "Resource": ["arn:aws:iot:us-east-2:*:topic/$aws/certificates/create/*",
                  "arn:aws:iot:us-east-2:*:topic/$aws/provisioning-templates/hyper-gateway-fleet/provision/*"]},
    {"Effect": "Allow", "Action": "iot:Subscribe",
     "Resource": ["arn:aws:iot:us-east-2:*:topicfilter/$aws/certificates/create/*",
                  "arn:aws:iot:us-east-2:*:topicfilter/$aws/provisioning-templates/hyper-gateway-fleet/provision/*"]}
  ]
}
```

The claim certificate can do nothing but request a gateway certificate. Anyone holding the
image could provision a thing with it, so keep the image in a private registry, and use a
pre-provisioning hook if that matters.

## 5. Assigning IMSs

[`tools/publish_ims.py`](../tools/publish_ims.py) assigns an IMS to a gateway. It uploads the
HAMF export to S3, sets `ims-<key>` to it, and adds the key to the `gateway` shadow's `ims`
list. The gateway installs it on its own and reports the result on `ims-<key>`. HAMF (or a
Lambda) can call the tool, or port it.

```bash
python tools/publish_ims.py --thing hyper-gw-SN-0042 --bucket <bucket> FWT-03.export.json
python tools/publish_ims.py --thing hyper-gw-SN-0042 --remove FWT-03
```

The download URL is presigned for `--url-ttl`, but a URL signed with temporary credentials
(SSO, an assumed role) stops working when they expire. Run the tool with credentials that
outlast the gateway's download. Telemetry is delivered at least once, so the consumer should
de-duplicate on `(gw, ims, ts)`.
