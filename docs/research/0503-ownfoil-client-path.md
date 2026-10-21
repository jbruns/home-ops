# Can Ownfoil be a Protected Service with a header-matched Client Path?

Spike for [#503](https://github.com/jbruns/home-ops/issues/503). Researched 2026-10-21. Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md). Mechanism/routing rules: [ADR 0006](../adr/0006-protected-services.md).

Checked against Ownfoil [`2.5.0`][of] (the pinned image), Sphaira [`1.0.8`][sp] and Traefik `v3.7.15` (edge's pin). Live facts come from read-only inspection of the Host. Ownfoil's own recorded client traffic stands in for Tinfoil, which is closed source.

## Answer

**Yes.** Ownfoil can be a Protected Service using forward auth as a gate. Its Switch shop clients get a Client Path, `ownfoil-client-switch`, matched on `HeaderRegexp(`Authorization`, `^Basic `)`.

- Tinfoil, Sphaira and CyberFoil all send `Authorization: Basic` pre-emptively, on every request, including the first. They never wait for a 401 challenge (Q1).
- The web UI never sends Basic itself. It signs in with a form and a session cookie (Q2).
- A local prototype passed: real Ownfoil 2.5.0 behind Traefik v3.7.15, with a stub IdP, replaying Ownfoil's recorded client traffic (Q3).
- `validate.py` already allows header matchers on `-client-` routers only, and its tests cover that (Q4).

A header matcher selects routing, not authorization. Anyone can add a Basic header to reach the Client Path. They then face Ownfoil's own sign-in, CrowdSec and a rate limit, which is no less protection than today's plain Exposed Service. Everyone else, including the scanners that make up most of `hnx` traffic today, meets the IdP first.

## Today

- `ownfoil-https` routes `Host(hnx)` through `crowdsec-bouncer` only. The Probe is `GET /login`.
- Live shop settings: `public: false`. Tinfoil (encrypted) and Sphaira are enabled; CyberFoil is disabled. Accounts come from `USER_ADMIN_*` and `USER_GUEST_*`.
- Traefik access logs for 19–21 Oct show no Switch client traffic on `hnx`. They contain the Probe plus about 70 scanner requests (`/.env*`, `/.git`, `/config`, …), so there is no live capture to compare against.

## Q1. Do the clients send credentials pre-emptively?

**Yes, all three.** Ownfoil's [recorded captures][caps] of real clients carry `Authorization: Basic` on the first request of every scenario that has credentials configured. They omit it only when the client has none:

| Client | First request | Requests seen | User-Agent |
|---|---|---|---|
| Tinfoil | `GET /` with Basic | `/`, `/<filter>/`, downloads | none on listings; `Mozilla/5.0 (Windows…)` on downloads |
| Sphaira 1.0.6 (HTTP mount) | `GET /` with Basic | `/`, `/<folder>/`, `HEAD`/`GET` files | `Sphaira/1.0.6` |
| CyberFoil | `GET /api/remote/sections` with Basic | `/api/remote/*`, `/api/shop/*`, `/`, downloads | `cyberfoil` |

Ownfoil's code points the same way: a challenge-waiting client could never work with it.

- Client `GET`s go through the [`authenticate`][of-client] and `verify_shop_access` decorators. Failures come back as a **200** body in the client's own format (JSON for Tinfoil, a directory listing for Sphaira), never a 401.
- [`file_access`][of-file] (downloads) returns 401 **without** `WWW-Authenticate`.

Sphaira's source confirms it for both of its modes:

- **HTTP mount** (what Ownfoil's `SphairaClient` serves): [`curl_easy_reset`][sp-mount] leaves libcurl's default `CURLAUTH_BASIC`, which sends credentials up front.
- **Native Ownfoil API** (new in 1.0.8):
  - The general curl setup uses [`CURLAUTH_ANY`][sp-any], which *would* wait for a challenge.
  - Every Ownfoil call opts into [`PreemptiveAuth{true}`][sp-api]: the `OPTIONS /` handshake, `POST /api/graphql`, and artwork when it comes from the shop.
  - Installs use [`yati::source::Http`][sp-yati] with `CURLAUTH_BASIC`.

Consequences for routing:

- **Match the `Authorization` header, not the User-Agent.** Tinfoil's downloads present a browser UA, and identifying UAs are as forgeable as any header anyway.
- The Client Path must cover **all paths** on the host. Sphaira's mount lists arbitrary folder paths, and the clients use `/api/download/<token>`, `/api/graphql` and `/api/media/…`.
- A client configured **without** credentials now gets the IdP's 302 instead of Ownfoil's "Shop requires authentication." It fails either way, but less legibly.

## Q2. Does the web UI ever use Basic auth?

**No, not on its own.** The UI signs in with a form `POST /login` and a Flask-Login session cookie. Its JavaScript never sets `Authorization`. Only two routes issue a Basic challenge ([`shop_access_denied`][of-denied]), and only when there is neither a session nor Basic:

- the `OPTIONS /` handshake, and
- `/api/media/…` artwork.

GraphQL deliberately omits the challenge.

Behind the gate, a browser without an IdP session never reaches those routes. A browser with an IdP session but an expired Ownfoil session could load artwork, get the challenge, and show a native Basic prompt, as it would today. If the user fills it in, the browser sends Basic for that artwork path. Those requests take the Client Path and Ownfoil checks them. That is harmless, and the prototype showed it is unreachable without the IdP.

## Q3. Prototype

The prototype ran Traefik `v3.7.15` with these routers, all on `Host(hnx.test)`:

- `ownfoil-https` → `protected`, where forward auth went to a stub that always 302s to an IdP.
- `ownfoil-health` → `Path(/site.webmanifest)`, using `strip-idp-headers`.
- `ownfoil-client-switch` → the header rule at priority 100, using `strip-idp-headers` and a rate limit.

Behind them was the pinned Ownfoil 2.5.0 image with a private shop. Ownfoil's captures were replayed with the prototype's credentials substituted:

| Request | Result |
|---|---|
| Tinfoil `GET /`, `/base/`: valid user | 200 from Ownfoil, `"Welcome to your own shop!"` listing |
| Tinfoil: wrong password, unknown user | 200 from Ownfoil, `"Shop requires authentication…"` |
| Tinfoil download UA (`Mozilla/5.0`) `GET /api/download/<token>` with Basic | Ownfoil (404 `No file with that token`, as expected for a fake token) |
| Sphaira 1.0.6 mount: `/`, folders, `HEAD`/`GET` files | Ownfoil directory listings |
| Sphaira 1.0.8 API: `OPTIONS /` handshake, `POST /api/graphql` | 200 from Ownfoil (handshake JSON, GraphQL data) |
| CyberFoil (disabled here and on the Host) | Ownfoil's own "disabled" error |
| Any client without credentials | 302 to the IdP |
| Browser `GET /`, `GET /login`, `POST /login`, `GET /api/media/…` | 302 to the IdP |
| Browser `GET /site.webmanifest` | 200 from Ownfoil (health router) |
| Junk Basic `GET /admin` / `POST /api/graphql` | Ownfoil: 302 to `/login` / 401 |
| 80 parallel junk-Basic requests | 40 × 200, 40 × 429 (rate limit) |
| `Authorization: basic …` (lowercase) | 302 to the IdP; every observed client sends `Basic` |

With the junk Basic header, every route that path reaches still needs an Ownfoil login. The realtime socket `/api/ws` [returns no topics without a session][of-ws]. Admin pages redirect to `/login`. Shop data needs a valid Basic user. So no path deny-list is needed. A deny-list would not narrow credential guessing either, since Basic on `/` is as good a password oracle as the login form.

The prototype could not test the real Authentik outpost, CrowdSec, TLS, or a real Switch.

## Q4. `validate.py`

`router_problems` rejects a header matcher on any router that is not `-client-` ([`validate.py`](../../tools/validate/validate.py)). `test_validate.py` covers both outcomes. #457 already did this; no change needed.

## Proposed configuration

The proposed labels for [`tn/ownfoil/compose.yaml`](../../tn/ownfoil/compose.yaml) pass `validate.py compose`:

```yaml
probe.url: https://hnx.${DOMAIN}/site.webmanifest
probe.idp_url: https://hnx.${DOMAIN}/

traefik.http.routers.ownfoil-https.middlewares: protected

traefik.http.routers.ownfoil-health.rule: Host(`hnx.${DOMAIN}`) && Path(`/site.webmanifest`)
traefik.http.routers.ownfoil-health.middlewares: crowdsec-bouncer,strip-idp-headers

traefik.http.routers.ownfoil-client-switch.rule: Host(`hnx.${DOMAIN}`) && HeaderRegexp(`Authorization`, `^Basic `)
traefik.http.routers.ownfoil-client-switch.priority: 100
traefik.http.routers.ownfoil-client-switch.middlewares: crowdsec-bouncer,strip-idp-headers,client-ratelimit
```

- **Probe:** `/site.webmanifest`, not `/login`. It is unauthenticated and carries no data. An exact `Path(/login)` health router would also publish `POST /login` without the IdP.
- **Priority:** Traefik's default priority (rule length) would already favour the client rule. The explicit 100 keeps the intent visible. The health router may lose to the client router for a Basic-carrying request; either router serves it correctly.

## Before switching the public router

These follow [#498's shared contract](https://github.com/jbruns/home-ops/issues/498) (recorded in `docs/research/0498-protected-services.md`):

1. Register `hnx` with the IdP and assign it to the edge outpost. Until then, the gated UI fails closed while the Client Path keeps working.
2. Back up `ownfoil_config`. Recovery is Ownfoil's own admin login over the Client Path's Basic route or a direct container shell; no unauthenticated UI is published.
3. On a real Switch, check Tinfoil and Sphaira: browse, install (download), wrong password. In a fresh browser, check: IdP, then Ownfoil login, then the library with artwork.
4. CrowdSec's HTTP brute-force scenarios will not see Ownfoil's failed shop logins, which return 200. The rate limit is the brake there, as it is today.

[of]: https://github.com/a1ex4/ownfoil/tree/2.5.0
[caps]: https://github.com/a1ex4/ownfoil/tree/2.5.0/tests/captures
[of-client]: https://github.com/a1ex4/ownfoil/blob/2.5.0/app/clients/client.py#L31-L72
[of-file]: https://github.com/a1ex4/ownfoil/blob/2.5.0/app/app.py#L230-L244
[of-denied]: https://github.com/a1ex4/ownfoil/blob/2.5.0/app/auth.py#L215-L221
[of-ws]: https://github.com/a1ex4/ownfoil/blob/2.5.0/app/realtime.py#L120-L131
[sp]: https://github.com/ITotalJustice/sphaira/tree/1.0.8
[sp-mount]: https://github.com/ITotalJustice/sphaira/blob/1.0.8/sphaira/source/utils/devoptab_common.cpp#L1370-L1374
[sp-any]: https://github.com/ITotalJustice/sphaira/blob/1.0.8/sphaira/source/download.cpp#L624-L625
[sp-api]: https://github.com/ITotalJustice/sphaira/blob/1.0.8/sphaira/source/utils/ownfoil_api.cpp#L191-L199
[sp-yati]: https://github.com/ITotalJustice/sphaira/blob/1.0.8/sphaira/source/yati/source/http.cpp#L54-L58
