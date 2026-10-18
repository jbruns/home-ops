# Every Service extends one Baseline; Exemptions are recorded and checked

Compose settings had drifted across Stacks: healthchecks, digest pins, `no-new-privileges`, capabilities and memory limits were each present on only some Services. The Baseline holds what every Service must carry: a healthcheck declared in Compose (even when the image has one, so an image change cannot silently drop it), `no-new-privileges`, a digest-pinned image, a memory limit, `restart: unless-stopped`, json-file logging at 10m x 3, and `cap_drop: [ALL]` with any needed `cap_add`. Its invariant settings live in one `baseline` service in `tn/_baseline.yaml`, which every Service pulls in with `extends: file:`. `validate.py` checks the rendered model, so the Baseline holds whether a setting came from `extends` or was written inline.

A Service that cannot meet an item records an Exemption under `x-baseline-exempt`, naming the item and the reason. CI rejects an Exemption without a reason and one for an item the Service already meets, and reports every Exemption, so the list stays auditable and shrinks as images improve. A Baseline item that most Services would need exempting is the wrong item. A fleet audit of 57 Services found that non-root users (about a quarter would need exempting) and read-only root filesystems (a dozen exemptions, plus a tmpfs list per Service) fail that test. Both buy isolation rather than much security here, so they are advisory, not Baseline.

Two consequences follow from sharing one file. A change to the Baseline recreates every container on the Host in one Deploy and ignores Startup Order, so Baseline changes ship alone in their own PR. doco-cd does not extend the Baseline and keeps those settings inline, so a Baseline change cannot race its self-update (#480, #481). doco-cd's deploy notifications do not list commits that touch only an extended file.

## Considered Options

- **A written convention, copied into every Service and enforced by `validate.py`**: rejected; it repeats the same block in every Service, and a change to it means editing every Stack.
- **YAML anchors or `x-` fragments**: rejected; they do not cross `include:` files, and every Stack is split that way.
- **A generator that renders the Compose files**: rejected; it is extra tooling for about twenty Stacks, and hides what is deployed.
