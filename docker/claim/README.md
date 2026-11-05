# Fleet claim certificate (not in git)

Put the fleet's AWS IoT **claim** certificate and key here before building the image:

    claim.cert.pem
    claim.private.key

They are copied into the image at `/opt/gateway-agent/claim/` (mode 0600, owned by the
Ignition user). A new gateway uses them once, on first boot, to obtain its own certificate
through fleet provisioning; after that they are not used. Their policy must allow only the
provisioning topics (see `docs/aws-fleet-provisioning.md`).

Anything in this folder other than this README is git-ignored.
