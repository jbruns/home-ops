# doco-cd deploys Stacks; Portainer is a viewer only

Portainer GitOps polling cannot decrypt SOPS, which forced decrypted secrets to be hand-placed beside its checkout on the Host. We deploy Stacks with doco-cd, which polls this repo and decrypts SOPS natively, so the repo is the only source of truth. Portainer stays on the Host only as a read-only viewer and as the hub for agents on other hosts; it must not own any Stack here. [#695](https://github.com/jbruns/home-ops/issues/695) supersedes the viewer and hub role: once asotbz/ops moved to doco-cd, Portainer managed nothing and was removed from the Host.

## Considered Options

- **Keep Portainer GitOps**: rejected; the SOPS workaround is manual and drifts, and 2.45 is the last 2.x long-term-support line.
- **Komodo**: rejected; heavier (Core, Periphery, and a database) and has no native SOPS support.
