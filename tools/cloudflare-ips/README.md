# cloudflare-ips

Keeps the Cloudflare edge ranges Traefik trusts for `X-Forwarded-For` in step with
[Cloudflare's published list](https://api.cloudflare.com/client/v4/ips). The ranges appear three
times in `tn/edge/traefik/service.yaml` (both entrypoints and the CrowdSec bouncer). Repo Tooling:
it never runs on the Host.

- `validate.py cloudflare` checks the three lists are valid CIDRs and identical.
- `--update` fetches the current ranges and rewrites all three lists if the set changed.
  [`cloudflare-ips.yaml`](../../.github/workflows/cloudflare-ips.yaml) runs it weekly and opens a PR.

## Run

```sh
python3 tools/cloudflare-ips/cloudflare_ips.py --update
python3 tools/cloudflare-ips/cloudflare_ips.py --update --file path/to/service.yaml
```

Standard library only.

## Configuration

- `--file PATH`: the Compose file holding the ranges (default `tn/edge/traefik/service.yaml`).
  It must contain exactly three `forwardedHeaders.trustedIPs=` / `forwardedHeadersTrustedIPs:` lists.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/cloudflare-ips
```

The tests stub the Cloudflare API; they need no network.
