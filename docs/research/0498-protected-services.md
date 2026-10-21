# Which existing Exposed Services should become Protected Services?

Spike for [#498](https://github.com/jbruns/home-ops/issues/498). Researched 2026-10-21.
Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md). Mechanism/routing rules: [ADR 0006](../adr/0006-protected-services.md).

## Answer

**Protect administrative UIs where feasible; an SSO button alone is insufficient.**
Native OIDC must send web UI users to the IdP by default and support a useful native-OIDC Probe.
Current docs are not proof of pinned-version support: autobrr needs a default-redirect feasibility check;
Tracearr's pinned auth routes do not contain the OIDC implementation described in current docs.

This checks upstream behavior against the repository's pinned versions and declared wiring,
not running instances. Sources identify checked tags where practical; unversioned docs are labelled.
Runtime integration audits and cutover acceptance remain prerequisites of each implementation.

| Existing Service / target | Protected? | Preferred feasible mechanism / follow-up |
|---|---|---|
| Grafana 12.4.12 | **Yes** | Native OIDC using Generic OAuth; [#580](https://github.com/jbruns/home-ops/issues/580) |
| Open WebUI v0.11.3 | **Yes** | Native OIDC; server login Probe, browser auto-redirect; [#575](https://github.com/jbruns/home-ops/issues/575) |
| autobrr v1.85 (source v1.85.0) | **Conditional** | Native OIDC exists, default redirect and redirect Probe not established; [#582](https://github.com/jbruns/home-ops/issues/582) |
| Navidrome v0.63.2 | **Yes** | Forward auth with identity handoff; Subsonic Client Path; [#576](https://github.com/jbruns/home-ops/issues/576) |
| Sonarr v4.0.20.3012 | **Yes** | Gate retaining Forms auth; `External` is not identity handoff; [#573](https://github.com/jbruns/home-ops/issues/573) |
| Radarr v6.4.4 | **Yes** | Same as Sonarr; API/feed Client Paths; [#579](https://github.com/jbruns/home-ops/issues/579) |
| SABnzbd 5.1.3 | **Yes** | Gate retaining UI credentials and API key; [#578](https://github.com/jbruns/home-ops/issues/578) |
| qBittorrent 5.2.4 | **Yes** | Gate only; retain native authentication; [#577](https://github.com/jbruns/home-ops/issues/577) |
| Syncthing 2.1 | **Yes** | Administrative GUI gate, native API-key authentication; [#574](https://github.com/jbruns/home-ops/issues/574) |
| Tracearr `supervised-1.4.27` | **Conditional** | Gate feasibility first; do not promise current-doc OIDC at this pin; [#583](https://github.com/jbruns/home-ops/issues/583) |
| Plex 1.43.4 | **No for now** | Preserve native account/token/client flows; no safe routing design established |
| Emby 4.9.5.0 | **No for now** | Preserve local/Connect users and native clients; no safe routing design established |
| NextPVR `stable` | **Defer / no change** | Identify immutable build and required clients, then bounded auth feasibility; [#581](https://github.com/jbruns/home-ops/issues/581) |
| nginx catch-all | **No** | No identity-bearing application UI |

### Existing work and exclusions

These statuses come from tracked configuration and the linked tickets, not a fresh deployed-state audit:
- Shelfmark v1.4.0 is **already native OIDC**, not forward auth: `AUTH_METHOD=oidc`,
  `OIDC_AUTO_REDIRECT=true`, `DISABLE_LOCAL_AUTH=true`, `probe.idp_auth=oidc`,
  `/api/auth/oidc/login`. Closed [#500](https://github.com/jbruns/home-ops/issues/500)
  concerns declarative settings; do not reopen the migration.
- Audiobookshelf is already native OIDC; [#501](https://github.com/jbruns/home-ops/issues/501)
  covers roles and its native Probe. CWA [#499](https://github.com/jbruns/home-ops/issues/499)
  moves current identity handoff to native OIDC.
- Ownfoil [#503](https://github.com/jbruns/home-ops/issues/503) is **conditionally** a gate,
  pending a header-matched Client Path prototype; do not turn that into an unconditional yes.
- AudioBookRequest is retired, absent from `tn/`; [#502](https://github.com/jbruns/home-ops/issues/502)
  is closed. Vaultwarden and Nextcloud are excluded Critical Services; Portainer is retiring.
  Traefik, Prometheus and Alertmanager are also Critical and outside this Warning/Info spike.
  Prowlarr is not deployed by this repository; assess it when it becomes a local Exposed Service.

## Shared implementation contract

For each implementation follow-up, record the mechanism, account/role mapping, exact Client Paths,
recovery procedure, and Probe changes before switching the public router:

1. Start with a fresh browser: UI reaches the IdP by default, unauthorized IdP users are denied,
   authorized users retain the intended account/data/role, and logout/re-login behaves correctly.
2. Gate Services retain their own login; identity handoff trusts only Traefik's actual source.
   No direct published GUI port may silently evade the intended policy.
3. Client Paths retain Service authentication, CrowdSec and rate limiting; test valid, invalid and
   missing credentials. A path or header matcher selects routing, **not authorization**.
   Strip IdP identity headers on every Client Path and exact-path forward-auth health router.
4. Test forged identity headers, WebSockets/long polls, necessary API clients and real client apps.
   Audit **persisted runtime** Downloader/indexer/cross-Service URLs and API keys, not just Compose.
5. Preserve a pre-cutover config/Datastore backup and tested restricted administrative recovery route.
   An IdP outage must not grant new unauthenticated access; existing valid sessions and deliberately
   retained native recovery credentials may still work. Recovery must not publish an unauthenticated UI.
6. Native OIDC: independently check an HTTP redirect from the Service's login initiation route
   to the expected IdP authorize endpoint, and unauthenticated health. Browser-side JavaScript
   navigation is not an HTTP 302 Probe. Label native Probes separately from outpost Probes.

**Declared local wiring:** Sonarr/Radarr exportarr and queue exporters use
`http://sonarr:8989` / `http://radarr:7878` with API keys on their Stack default networks.
SAB exporters use `http://sabnzbd:8080`; Sonarr/Radarr → SAB dependency Probes use
`http://sabnzbd:8080/api?mode=version` through the downloads blackbox exporter.
No Sonarr↔Radarr calls are declared. These establish declared wiring, **not live application URLs**.
Evidence: [Sonarr](../../tn/sonarr/sonarr/service.yaml),
[Radarr](../../tn/radarr/radarr/service.yaml), [SABnzbd](../../tn/sabnzbd/sabnzbd/service.yaml),
[Dependency Probes](../../tn/monitoring/prometheus/dependencies.yml) and
[blackbox-exporter networks](../../tn/monitoring/blackbox-exporter/service.yaml).
The [edge configuration](../../tn/edge/traefik/service.yaml) assigns Traefik `172.30.0.10`;
verify the observed source before trusting it for identity handoff.
Persisted application URLs are not declared here: each implementation issue must audit them.

## Grafana 12.4.12 — yes, native OIDC

The [12.4 Generic OAuth docs][g-doc] describe role mapping and auto-login;
the [v12.4.12 defaults][g-config] and [provider implementation][g-source] confirm the knobs.
Configure `[auth.generic_oauth] enabled=true`, `auth_url`, `token_url`, `api_url`, credentials,
`scopes=openid profile email`, `use_pkce=true`, and `auto_login=true`.
Register the callback `/login/generic_oauth`; ensure the external root URL is correct.

Map claims deliberately: `login_attribute_path`, `email_attribute_path`, `name_attribute_path`,
`groups_attribute_path`, `allowed_groups`, and `role_attribute_path`.
The pinned implementation **does support `allowed_groups`**; it requires `groups_attribute_path`.
Use `role_attribute_strict=true` and an expression that yields no valid role for disallowed groups;
an expression ending `|| 'Viewer'` intentionally admits everyone as Viewer, not denial.
`Admin` is an organization role; only enable `allow_assign_grafana_admin` if server-admin mapping
is genuinely intended. Test claim shape and existing-user linking; do not enable insecure email matching.

Keep local password auth and an established administrator for recovery:
[`/login?disableAutoLogin=true`][g-login] suppresses auto-login; it does not bypass credentials.
Probe `/login/generic_oauth` for the IdP redirect and `/api/health` for health.
Preserve native API/service-account credentials for non-browser callers; no Client Path is needed.
Acceptance: allowed/disallowed groups, admin vs Viewer, existing dashboards/account ownership,
the local recovery login, API tokens, health, and the actual authorize Location.

## Open WebUI v0.11.3 — yes, native OIDC

The [tagged config][ow-config], [browser login page][ow-page], [OAuth manager][ow-oauth],
and [server routes][ow-main] establish behavior independently of the [current SSO docs][ow-doc].
Set `ENABLE_OAUTH=true`, discovery URL `OPENID_PROVIDER_URL`, client ID/secret,
`OAUTH_SCOPES=openid email profile`, and `ENABLE_OAUTH_SIGNUP=true` for first-login provisioning.
Set `OAUTH_AUTO_REDIRECT=true`, `ENABLE_LOGIN_FORM=false`, exactly one OAuth provider and no LDAP.
The browser auto-redirect is **JavaScript**, suppressed by `?form=true`, errors and some logout states.
Use **`/oauth/oidc/login`**, not `/auth`, for the HTTP-redirect native-OIDC Probe;
register `/oauth/oidc/login/callback`. The existing health path is `/health`.

Set `ENABLE_OAUTH_ROLE_MANAGEMENT=true`, `OAUTH_ROLES_CLAIM`, `OAUTH_ALLOWED_ROLES`,
`OAUTH_ADMIN_ROLES`; align these with explicit IdP access policy. Audit bootstrap/single-user admin
behavior and existing accounts before testing with a new user. Email merging via
`OAUTH_MERGE_ACCOUNTS_BY_EMAIL` needs verified email ownership; do not assume the IdP enforces it.
For recovery retain a local admin password and `ENABLE_PASSWORD_AUTH=true`:
[`/auth?form=true`][ow-page] exposes the form, but [password sign-in][ow-signin] still checks
`ENABLE_PASSWORD_AUTH`. Hiding the form alone does not disable the backend password endpoint.
Preserve API-key callers and test existing keys, chats/files, role denial/promotion, recovery,
browser auto-redirect, HTTP login redirect and health. No proxy Client Path is required.

## autobrr v1.85.0 — native OIDC exists; Protected-by-default is conditional

[Tagged OIDC service][ab-service] discovers the IdP and enables S256 PKCE when advertised.
[Tagged HTTP handler][ab-http] validates tokens and establishes the application's authenticated session.
Configure `oidcEnabled`, `oidcIssuer`, `oidcClientId`, `oidcClientSecret`, `oidcRedirectUrl`
(`/api/auth/oidc/callback`); environment equivalents use `AUTOBRR__OIDC_*`
([current upstream configuration guide][ab-doc]).

**Blocking mismatch:** [the pinned Login component][ab-page] fetches OIDC configuration and renders
an OIDC button whose click navigates to `authorizationUrl`. `oidcDisableBuiltInLogin` hides the
password form; it does **not** itself auto-navigate. `/api/auth/oidc/config` returns JSON, not an
HTTP redirect to the IdP. Do not label `/login` or that JSON route as a working redirect Probe.
All accepted OIDC identities receive the same application control; enforce operator membership at
the IdP, not fictitious application role mapping. Keep the existing local account for recovery.

Bounded feasibility follow-up: demonstrate default navigation **and** a safe HTTP initiation route
with state/PKCE preserved using supported upstream behavior/a suitable newer pin. If unavailable,
choose a gate retaining local login under ADR 0006; do not add an ad hoc auth adapter as a shortcut.
Test Secure cookies behind `X-Forwarded-Proto: https`, callback/state failures, denied IdP users,
API-key callers, rollback to built-in login, and the existing health path `/api/healthz/liveness`.
Only then commit to native `probe.idp_auth=oidc` and its verified route.

## Navidrome v0.63.2 — yes, identity handoff and Subsonic Client Path

[Tagged auth implementation][nd-source] validates the proxy IP before reading `ExtAuth.UserHeader`.
[Current external-auth docs][nd-doc] explain account creation, password fallback and Subsonic behavior.
Set `ND_EXTAUTH_TRUSTEDSOURCES=172.30.0.10/32` **after verifying that is the source Navidrome sees**;
the owner supplied Traefik's static address. Prefer this exact address to the whole proxy network.
Set `ND_EXTAUTH_USERHEADER=X-Authentik-Username`; optionally `ND_EXTAUTH_LOGOUTURL`.

Match existing usernames exactly. Unknown proxy identities auto-create users with random passwords;
the first user on an empty database becomes admin, so preserve existing admin state and test roles.
Keep user password management available where Subsonic clients require native credentials.
No IdP-group-to-admin mapping is established here; manage Navidrome roles explicitly.

Route `/rest/` as `navidrome-client-subsonic`, strip identity headers and let Navidrome validate
`u`/`p` or token/salt credentials. Do not mistake HTTP 200 with a Subsonic error for successful login.
The web UI also uses Subsonic: the docs explicitly allow either proxy-authenticating `c=NavidromeUI`
or letting those calls use the credentials the web app receives from Navidrome.
Choose and test one; it is **not** necessary to assert that routing all `/rest/` calls to native
authentication breaks the web UI. Shared links `/share/` need separate approval, not a silent exemption.
Existing health path: exact `/ping`. Recovery: restricted direct access with known local admin
credentials and rollback of ExtAuth settings. Test third-party playback, web playback,
username/account preservation, wrong passwords, and forged headers from both Client Paths and peer IPs.

## Sonarr / Radarr — yes, gate; External is not identity handoff

Pinned [Sonarr auth registration][so-auth] and [Radarr auth registration][ra-auth] map
`External` to `NoAuthenticationHandler`: **neither consumes a trusted identity header**.
Calling that identity handoff would contradict ADR 0006's definition.
The [upstream Sonarr FAQ][so-doc] warns that External disables application authentication.
Choose the existing ADR's gate: `AuthenticationMethod=Forms`, `AuthenticationRequired=Enabled`,
strong local administrative credentials, forward auth on the web UI.
There is no per-IdP-user account/role model to synchronize in these single-operator interfaces.

If removing the second login is later desired, separately decide whether ADR 0006 should admit
external-auth-without-identity semantics; do not change the ADR implicitly in an implementation ticket.
The [pinned API handler][so-api] independently checks `X-Api-Key` / `apikey` (also Bearer).
External UI auth is **not** proof that every API becomes unauthenticated; test the actual API policy.

Create narrow API Client Paths for `/api/` (and exact `/api` if required), and calendar `/feed/`
only where required. Use each generated calendar URL and its API key; verify missing/wrong keys fail.
Do not use an unbounded `/api*` prefix or exempt all UI/static routes.
Both existing health paths are exact `/ping`. Recovery: retain local credentials and revert
the forward-auth router via restricted access. Acceptance includes native Forms login behind the gate,
authorized API/feed use without an IdP session, denied missing/wrong keys, and exporter continuity.
Audit persisted Downloader/indexer/cross-Service URLs before rollout; declared direct callers above
do not establish that all running integrations avoid Traefik.

## SABnzbd 5.1.3 — yes, gate and API Client Path

[Tagged configuration][sab-config] defines `username`, `password`, `html_login` and `config_lock`;
[tagged API authentication][sab-source] checks the full API key, or an NZB key for restricted calls.
There is no demonstrated native OIDC/header identity mechanism in the checked integration.
Keep non-empty UI credentials, `html_login=true`, and the existing API key behind forward auth.
`config_lock` is optional hardening with functional consequences, not a replacement for auth.
Map IdP access to operators; SAB itself has shared UI credentials, not IdP-derived user roles.

Expose exact `/api` as `sabnzbd-client-api`, preserving API-key checks and limits.
`mode=version` and `mode=auth` are explicitly keyless in this pinned source; all other calls require
appropriate keys. Therefore the existing health path `/api?mode=version` is usable for unauthenticated health.
A health router must match both exact path **and** exact mode; a path-only `/api` health exemption
is not an exact functional health route. Never log raw API-key query strings.
Recovery: restricted access to the existing native login; restore saved configuration if necessary.
Acceptance: gate + local login, API success/denial, minimal NZB-key privileges, version health,
exporters and actual Organizer download submission. No unrelated vulnerability claim is assumed cleared.

## qBittorrent 5.2.4 — yes, gate only; never subnet authentication bypass

[Tagged WebApplication][qb-source] shows that `isAuthNeeded()` skips native authentication for
loopback or configured subnet exemptions. It also separately supports `Authorization: Bearer`
API-key sessions, rejecting API-key access to static WebUI files and auth controller operations.
Keep native credentials; set `WebUI\LocalHostAuth=true` and
`WebUI\AuthSubnetWhitelistEnabled=false` (disable both localhost and subnet exemptions).
Reverse-proxy trust for forwarded IPs is **not** authorization to skip login.

No native OIDC or identity-header handoff is established by this source; choose a gate.
The IdP admits operators; qBittorrent retains its single native administrative identity.
Automation may use a narrow `/api/v2/` Client Path with native API-key or session-login authentication.
Check each existing client for native API-key versus session-login support before choosing the mechanism.
Missing/invalid keys must fail; keep native CSRF/Host protections and test cookies/origin behavior.
The existing health path, exact `/`, should return only the native login page, not torrent data;
it is intentionally a shallow reachability check, not evidence that authenticated operations work.
Recovery: known native credentials through restricted access plus config/router rollback.
Acceptance: IdP gate, second login, no subnet/localhost exemption, native API client operations,
spoofed proxy headers, missing credentials, and unauthenticated `/` without data disclosure.

## Syncthing 2.1 — yes, administrative GUI gate, not sync-traffic authentication

[v2.1.0 auth modes][st-mode] are `static` and `ldap`, not trusted identity-header/OIDC modes.
[Tagged GUI configuration][st-config] and [auth middleware][st-auth] confirm native GUI credentials,
sessions and API-key authentication. [Versioned reverse-proxy docs][st-doc] describe sharing front-end
authentication, but that does not imply Syncthing consumes a proxy username.
Choose a gate; keep `<gui><user>`, hashed `<password>`, `<authMode>static</authMode>` and `<apikey>`.
Do not invent `guiAndFileAuth` settings or disable GUI auth to simulate identity handoff.

This is an administrative interface, not per-IdP-user authorization: allow only operators at the IdP.
File synchronization/device trust runs separately and must not be routed through the web gate.
If external REST callers exist, use a `syncthing-client-api` route limited to required `/rest/`
endpoints with native `X-API-Key` authentication. Do not exempt the whole GUI.
Health: exact `/rest/noauth/health`; avoid making the whole `/rest/noauth/` namespace a health router.
Recover through restricted GUI access with native credentials; restore backed-up GUI config if needed.
Acceptance: GUI gate + native login, wrong/missing REST key denial, working API-key monitoring,
event/long-poll behavior and unaffected device synchronization. Confirm the exact deployed 2.1 patch.

## Tracearr supervised-1.4.27 — conditional gate, not verified native OIDC

**Version matters:** [current member-sign-in docs][tr-doc] describe OIDC, but the [v1.4.27 auth
registration][tr-auth] registers local, Plex, Jellyfin, Emby and session routes, not an OIDC module.
[Pinned local login][tr-local] accepts local/Plex login types; initial local signup creates an owner.
Do not copy current `OIDC_*` knobs into this pinned Service and declare them effective.
An upgrade to a verified OIDC-capable immutable version is a separate feasibility option.

For this pin, prototype a gate retaining existing local/media-account login and existing roles.
[Pinned server registration][tr-server] includes `/health` and `/api/v1/mobile`;
[mobile routes][tr-mobile] include pairing and authenticated mobile operations.
Inventory required mobile clients before opening Client Paths: a blanket `/api/v1/` exemption
could leave most of the administrative interface reachable outside the IdP.
Use authenticated native-token/header routes only where demonstrated safe; pairing/bootstrap routes
need their own abuse/authorization checks. Browser Plex callbacks must survive the gate.
Acceptance: owner recovery via native login over restricted access, correct existing roles,
Plex/Jellyfin/Emby flows actually in use, mobile pairing/refresh/API/WebSocket behavior, denied native
credentials and exact `/health`. If no narrow safe client design exists, defer protection at this pin.
Only choose native OIDC after verifying tagged implementation, default navigation and HTTP Probe.

## Plex / Emby / NextPVR — conservative no-for-now, not impossibility claims

**Plex:** [official network documentation][plex-doc] describes account-based clients and reverse-proxy
advertising through custom access URLs. It explicitly warns that its no-auth network list grants
admin/owner access. Do not use that setting as a handoff mechanism.
At 1.43.4, no verified native-OIDC/trusted-header integration or narrow gate/Client Path design is
established here. Preserve native client/token flows rather than adding a blanket gate that breaks them.

**Emby:** [official user docs][emby-doc] describe server-managed local users, passwords, permissions
and optional Emby Connect linkage. Those are not generic IdP identity handoff.
These are current docs (they also mention 4.10 features), not proof of OIDC absence in 4.9.5.0.
Keep native client authentication for now; reconsider after a version-specific supported integration
or tested web/client route separation exists. Neither decision claims a gate is universally impossible.

**NextPVR:** the immutable version behind `stable` is unknown. The [official Kodi client guide][np-doc]
describes backend port/PIN configuration and live-TV/recording clients, but is not a modern
version-specific server-auth contract. Do not infer either OIDC support or safe Client Paths from it.
Bounded follow-up: identify image digest/build, enumerate clients and API/stream/session routes,
verify server authentication at that build, prototype gate + narrow authenticated Client Paths,
and test playback, recordings, guide updates and recovery before recommending protection.

**nginx catch-all:** no application identity UI per the owner's inventory; leave unprotected.

## Sources and remaining verification

All links below are first-party docs/source. Tagged source was read directly, not inferred from
search summaries. Current docs are explicitly distinguished above. No deployed sign-in was exercised.
Runtime URL/key audits and deployment acceptance remain outstanding in the implementation tickets;
autobrr, Tracearr, Ownfoil and NextPVR retain explicit feasibility gates.

[g-doc]: https://grafana.com/docs/grafana/v12.4/setup-grafana/configure-security/configure-authentication/generic-oauth/
[g-config]: https://github.com/grafana/grafana/blob/v12.4.12/conf/defaults.ini
[g-source]: https://github.com/grafana/grafana/blob/v12.4.12/pkg/login/social/connectors/generic_oauth.go
[g-login]: https://github.com/grafana/grafana/blob/v12.4.12/pkg/api/login.go
[ow-config]: https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/config.py
[ow-page]: https://github.com/open-webui/open-webui/blob/v0.11.3/src/routes/auth/+page.svelte
[ow-oauth]: https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/utils/oauth.py
[ow-main]: https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/main.py
[ow-signin]: https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/routers/auths.py
[ow-doc]: https://docs.openwebui.com/features/authentication-access/auth/sso/
[ab-service]: https://github.com/autobrr/autobrr/blob/v1.85.0/internal/auth/oidc.go
[ab-http]: https://github.com/autobrr/autobrr/blob/v1.85.0/internal/http/oidc.go
[ab-page]: https://github.com/autobrr/autobrr/blob/v1.85.0/web/src/screens/auth/Login.tsx
[ab-doc]: https://autobrr.com/configuration/authentication
[nd-source]: https://github.com/navidrome/navidrome/blob/v0.63.2/server/auth.go
[nd-doc]: https://www.navidrome.org/docs/usage/integration/authentication/
[so-auth]: https://github.com/Sonarr/Sonarr/blob/v4.0.20.3012/src/Sonarr.Http/Authentication/AuthenticationBuilderExtensions.cs
[ra-auth]: https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/Radarr.Http/Authentication/AuthenticationBuilderExtensions.cs
[so-api]: https://github.com/Sonarr/Sonarr/blob/v4.0.20.3012/src/Sonarr.Http/Authentication/ApiKeyAuthenticationHandler.cs
[so-doc]: https://wiki.servarr.com/sonarr/faq-v4
[sab-config]: https://github.com/sabnzbd/sabnzbd/blob/5.1.3/sabnzbd/cfg.py
[sab-source]: https://github.com/sabnzbd/sabnzbd/blob/5.1.3/sabnzbd/interface.py
[qb-source]: https://github.com/qbittorrent/qBittorrent/blob/release-5.2.4/src/webui/webapplication.cpp
[st-mode]: https://github.com/syncthing/syncthing/blob/v2.1.0/lib/config/authmode.go
[st-config]: https://github.com/syncthing/syncthing/blob/v2.1.0/lib/config/guiconfiguration.go
[st-auth]: https://github.com/syncthing/syncthing/blob/v2.1.0/lib/api/api_auth.go
[st-doc]: https://docs.syncthing.net/v2.1.0/users/reverseproxy.html
[tr-doc]: https://docs.tracearr.com/configuration/member-sign-in
[tr-auth]: https://github.com/connorgallopo/Tracearr/blob/v1.4.27/apps/server/src/routes/auth/index.ts
[tr-local]: https://github.com/connorgallopo/Tracearr/blob/v1.4.27/apps/server/src/routes/auth/local.ts
[tr-server]: https://github.com/connorgallopo/Tracearr/blob/v1.4.27/apps/server/src/index.ts
[tr-mobile]: https://github.com/connorgallopo/Tracearr/blob/v1.4.27/apps/server/src/routes/mobile.ts
[plex-doc]: https://support.plex.tv/articles/200430283-network/
[emby-doc]: https://emby.media/support/articles/Users.html
[np-doc]: https://kodi.wiki/view/Add-on:NextPVR_PVR_Client
