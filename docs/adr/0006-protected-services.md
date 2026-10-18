# Protected Services: one outpost in edge, native OIDC first, Client Paths for required clients

The asot-media Services were the first Protected Services. Their forward-auth outpost and middleware lived inside that Stack, but more Services will sign in at the IdP later, so this pattern is now shared. The proxy outpost runs in the edge Stack, which defines one role-named forward-auth middleware and a shared chain with the CrowdSec bouncer. Traefik carries those definitions rather than the outpost, so a missing outpost fails closed. A Service is Protected by one of three mechanisms, in order of preference:

1. native OIDC, where the Service redirects to the IdP itself and needs neither the outpost nor any Client Path,
2. forward auth with identity handoff, where the Service trusts the outpost's identity header from Traefik's network only,
3. forward auth as a gate, where the Service keeps its own login behind the outpost.

Each Service records its mechanism and the reason for it in its Compose file. When a required non-browser client cannot sign in at the IdP (OPDS readers, Switch shop clients, Subsonic or Xtream clients), a Client Path routes it past forward auth to the Service's own sign-in, behind the CrowdSec bouncer and a rate limit. A Client Path is matched by path or, when the client shares the UI's path, by header. Its router is named `<service>-client-<protocol>`, after the protocol or device class rather than the client product, so swapping Tinfoil for Sphaira renames nothing. Every forward-auth Service also has an exact-path `<service>-health` router that skips the IdP for its Probe.

The outpost is Critical, because every Protected Service fails with it and the Host can fix it. The IdP runs on another machine and stays Warning, as an explicit exception to the rule that a Dependency takes the highest Tier among its dependents: an IdP outage is that machine's to Page on. While the IdP's DependencyDown is firing, it inhibits the outpost's lost-connection alert. The IdP Probe (a 302 to the authorize endpoint) is answered by the outpost, so the IdP itself is also Probed as an External Dependency. A native-OIDC Service's IdP Probe is answered by its own login path instead, so it is labelled apart and never counts towards the outpost's alert.

A Critical Service is not Protected by default. Making it Protected would make the IdP a Dependency of a Critical Service and risk locking users out, so it is decided per Service by its audience. Vaultwarden and Nextcloud stay off the IdP for now. If a Critical Service does become Protected, revisit the IdP's Tier.

## Considered Options

- **Outpost in its own Stack, or one per app Stack**: rejected; it is on the request path like Traefik and CrowdSec, and one per app multiplies tokens and Probes.
- **Forward auth for every Protected Service**: rejected; native OIDC removes the outpost from the path and keeps native mobile apps working without Client Paths.
- **Paging on the IdP**: rejected while every Protected Service is Warning; the IdP is outside the Host.
