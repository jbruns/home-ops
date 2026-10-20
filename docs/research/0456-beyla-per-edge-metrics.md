# Can Beyla give per-edge RED metrics and a service graph between Services, with no app changes?

Spike for [#456](https://github.com/jbruns/home-ops/issues/456), part of [#421](https://github.com/jbruns/home-ops/issues/421). Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md). Hardening constraints: [#422](https://github.com/jbruns/home-ops/issues/422) and [ADR 0007](../adr/0007-service-baseline-via-extends.md). Host-update rules: [ADR 0003](../adr/0003-startup-order-via-host-scripts.md).

Researched 2026-10-20 against the Host:
- Versions: TrueNAS 25.10.4, kernel 6.12.91, Docker 28.3.1, cgroup v2.
- The current Alloy pin, `grafana/alloy:v1.20.1`, bundles Beyla v3.35.0 (OBI SDK v1.46.0). The standalone `grafana/beyla:3.35.0` was used to rule out the Alloy wrapper.
- Every run was a throwaway `docker run` named `beyla-spike` with `--label startup-order=skip` and `--cap-drop=ALL` plus explicit caps. No Compose file, Host setting or Service changed. All runs and files were removed afterwards.

## Answer

**No-go.** Beyla runs on this Host without OS changes and sees real edges, but what it adds doesn't justify what it costs:

- **The edge we most wanted stays unattributed.** The prize was real-traffic RED metrics for External Dependency calls over HTTPS, such as Sonarr → nzbhydra2. Beyla does see Sonarr's outbound HTTPS, but labels the peer `outgoing` instead of naming it. Sonarr has two HTTPS Dependencies (nzbhydra2 and the IdP), so the series can't even be assigned to one of them ([E4](#e4-features-env-15-min)).
- **The edges it does name add only real-traffic rates over what we have.** Internal HTTP edges like `sonarr → sabnzbd:8080` come out cleanly, with Compose service names. The Dependency Probes in `tn/monitoring/prometheus/dependencies.yml` already watch those edges and Sonarr/Radarr → nzbhydra2, and exportarr covers the apps. Probes are synthetic: they give up/down and latency, not the dependent's actual request rate and errors. #421 already accepted that trade-off, and Beyla's extra detail doesn't justify the costs below.
- **There is no service graph.** `traces_service_graph_*` stayed at zero series in every configuration, both in Alloy and in standalone Beyla ([E4, E5](#e5-standalone-beyla)).
- **It needs privileges #422 is removing.** `SYS_ADMIN`, which is effectively root on the Host, and raw Docker API access, which #422 limits to a few Services. Root, the other caps and host PID meet the Baseline as written (ADR 0007: non-root is advisory, `cap_add` with a reason is allowed), but together they make Beyla the most privileged Service on the Host ([privileges](#privileges-and-the-baseline)).
- **Teardown blocks for minutes.** Stopping Beyla left a zombie container for 2–9.5 minutes while the kernel removed uprobes ([teardown](#teardown-blocks-for-minutes)). Every doco-cd recreate would wait on that, and the upper end is longer than the 300 s Shutdown script timeout.
- **The cost is large for what it adds.** 5–20k series depending on attributes and run length (about 15k in a realistic config), 0.13–0.5 of a core, and 0.65–1.3 GiB, compared with Alloy's current 384M limit ([cost](#cardinality-and-resources)).
- **There are correctness bugs.** Go clients report garbage `server_port` values, and blackbox-exporter reports bogus IPv6 `server_address` values ([accuracy](#accuracy)).

**Reopen if** all of the following hold:
1. OBI names TLS peers. Upstream issues: open-telemetry/opentelemetry-ebpf-instrumentation#994 and open-telemetry/opentelemetry-ebpf-instrumentation#3281.
2. The service graph produces series outside Kubernetes.
3. A Beyla release has cheap uprobe teardown.

Re-test with the then-current Alloy pin using the [appendix](#appendix-experiments) configs.

## Questions from #456

### Kernel and BTF

The kernel supports Beyla out of the box, so no OS change is needed and ADR 0003 holds.
- `/sys/kernel/btf/vmlinux` is present.
- `CONFIG_DEBUG_INFO_BTF`, `BPF_SYSCALL`, `BPF_JIT`, `CGROUP_BPF`, `KPROBES` and `UPROBES` are all `=y`.
- Lockdown is `[none]`.

The remaining update risk is the usual one: a TrueNAS kernel bump could change BTF or uprobe behaviour under a pinned Beyla.

### Privileges and the Baseline

| Need | Why | Without it |
|---|---|---|
| `BPF`, `PERFMON`, `NET_ADMIN`, `NET_RAW` | load eBPF programs and socket filters | won't start |
| `DAC_READ_SEARCH`, `SYS_PTRACE`, `CHECKPOINT_RESTORE` | read other processes' binaries and `/proc` | no attachment |
| `SYS_ADMIN` | Go and TLS uprobes; joining a target netns | no HTTPS, no Go clients, "join target netns: operation not permitted" (E1, E2) |
| `--pid=host` | see other containers' processes | sees only itself |
| `--user 0:0` | upstream requires root, or file caps `+ip` | — |
| Docker API (`/info`, container inspect, `/events`) | name Services | `service_name` = process name (`python3.14`, `haproxy`), `instance` includes the PID (E1) |

Results under Baseline settings (E6):
- **`no-new-privileges` works:** caps added to a root process survive it.
- **A bridge network works too** for application metrics. Host network is only needed for network flows, which turned out to be worthless here ([accuracy](#accuracy)).
- Docker's default AppArmor and seccomp profiles didn't block anything.

**Docker API through the proxy.** Beyla only needs the read-only Docker API routes listed in the table. The docker-socket-proxy planned in #422 could therefore serve them with `INFO`, `CONTAINERS` and `EVENTS` enabled, through `DOCKER_HOST`. This was not tested.

**Against the Baseline and #422** (ADR 0007):
- Root, `cap_drop: ALL` plus seven `cap_add`s with reasons, host PID and `no-new-privileges` all meet the Baseline as written. Non-root is advisory, and PID namespace isn't a Baseline item.
- The real conflicts are with #422's goals. `SYS_ADMIN` is effectively root on the Host. Raw Docker API access is something #422 restricts to a few Services, though the proxy route above would avoid it.

### Which edges are visible

| Edge kind | Example | Seen? |
|---|---|---|
| Internal HTTP on Docker bridges | `sonarr → sabnzbd:8080`, `radarr → sabnzbd:8080`, exporters → Services, Prometheus scrapes | yes, named by Compose service (E2–E6) |
| Traefik → backend | `traefik → 172.30.0.150:8989` (= Sonarr) | yes, but **named by IP** |
| External HTTPS to a known host | `idp-outpost → idp.asot.bz:443`, `doco-cd → github.com`, `alertmanager → discord.com`, `crowdsec → api.crowdsec.net`, `plex → plex.direct` | yes, needs `SYS_ADMIN` (E3) |
| External HTTPS from Sonarr/Radarr (.NET) | Sonarr → nzbhydra2 or the IdP | **captured but peer = `outgoing`**, so it can't be told apart (E4) |
| Network flows (`network` feature) | any TCP | IPs only outside Kubernetes; NAT shows each external flow twice (container IP and Host IP) (E2) |

### Accuracy

- **Go HTTP clients:** `server_port` comes out as garbage, e.g. `18640` or `52428` (E3).
- **blackbox-exporter:** `server_address` is a bogus IPv6 address. This matches open-telemetry/opentelemetry-ebpf-instrumentation#3281 ("SSL pointer rendered as IPv6").
- **TLS peers:** some come out as `outgoing`, per open-telemetry/opentelemetry-ebpf-instrumentation#994.
- **Go binaries without SYS_ADMIN:** many log "Unsupported Go program, using generic instrumentation" and raise `beyla_instrumentation_errors_total{error_type=attaching_uprobe|symbol_not_found}` (E1).

### Service graph

`application_service_graph` was enabled in three ways, and none produced a single `traces_service_graph_*` series:
1. Alloy `metrics.features` (E1–E3);
2. the `OTEL_EBPF_METRICS_FEATURES` env var (E4);
3. standalone Beyla with both `metrics.features` and `prometheus_export.features` (E5).

`features: ["*"]` exits at start-up. It pulls in the stats feature, which needs tracefs, and the container doesn't mount tracefs.

The root cause wasn't found. The OBI source gates the graph on per-Service features and on whether span metrics were observed. Either way, the graph would inherit the `outgoing` and IP-naming gaps above.

### Cardinality and resources

| Run | Config | Series | CPU (of 1 core) | RSS |
|---|---|---|---|---|
| E1 | default attrs, no Docker API, 3 min | ~5.4k | 13% | 917 MiB |
| E2 | Docker API, `dst.port` in flows, 11 h | 9.8k (5.7k flows) | ~35% | 1.26 GiB |
| E3 | + `SYS_ADMIN`, 5 min | ~20k (~40% body-size histograms) | ~49% | ~900 MiB |
| E4 | E3 + env features, 15 min | 15.3k | ~22% | 650 MiB |
| E5 | standalone, application + service graph | 1,755 | — | — |
| E6 | standalone, application only, NNP + bridge, 2.5 min | 15.0k | — | — |

The gap between E5 and E6 wasn't explained: E5 didn't record how long it had been running, and both used the same discovery config. Read E5 as a lower bound from early in attachment.

- **Stability:** E2 ran 11 hours unattended with no restarts and no OOM.
- **Cardinality controls:**
  - `dst.port` in flows picks up ephemeral ports and drives most of the flow cardinality.
  - In Alloy, an `attributes.select` `include` list *replaces* the defaults. Leaving `service_name` out of it drops it from every client series.
- **Current monitoring stack, for comparison:** Alloy is capped at 384M today. Prometheus would take on roughly 15k extra series in a realistic config (E4, E6), and up to 20k with default histograms (E3).

### Teardown blocks for minutes

- **What happens:** when Beyla stops, the exiting thread sits in `D` state in `perf_uprobe_destroy ← perf_event_release_kernel ← do_exit`. The main thread is a zombie, and `docker stop`/`docker rm -f` fail with "PID is zombie and can not be killed".
- **E5:** this cleared about 2 minutes after the kill.
- **E6 (all containers instrumented):** this took ~9.5 minutes.
- **Impact:**
  - **doco-cd:** every recreate blocks for that long.
  - **Host shutdown:** the startup-order Custom Component's Shutdown script has a 300 s timeout (`host/README.md`). E6's teardown is longer than that, so the timeout either fires first or Beyla is still exiting when TrueNAS stops dockerd.
  - **Startup Order:** Beyla would have to stop first, or be excluded.

### Surviving TrueNAS updates (ADR 0003)

Beyla is container-only: there's no kernel module, no sysctl change and no Host package. That fits ADR 0003. What could break is Beyla itself after a kernel update; an eBPF attachment failure takes no Service down with it. The one update-path cost is the reboot: see the Shutdown timeout under [teardown](#teardown-blocks-for-minutes).

## Appendix: experiments

Common flags for every run:

```sh
docker run -d --name beyla-spike --label startup-order=skip \
  --pid=host --user 0:0 --cap-drop=ALL \
  --cap-add=BPF --cap-add=NET_ADMIN --cap-add=NET_RAW --cap-add=PERFMON \
  --cap-add=DAC_READ_SEARCH --cap-add=SYS_PTRACE --cap-add=CHECKPOINT_RESTORE \
  ...
```

### E1: Alloy, no Docker API

- **Setup:** `--network=host`, no `SYS_ADMIN`, no socket. `beyla.ebpf` with features `application`, `application_service_graph`, `network`.
- **Coverage:** attached to ~45 processes (Go, .NET, Python, Node, C++).
- **Edges:** captured `http_client_request_duration_seconds{service_name="Sonarr",server_address="sabnzbd",server_port="8080",http_response_status_code="200"}`.
- **Errors:** `join target netns: operation not permitted` and `failed to enable runtime stats`.

### E2: Alloy, Docker API

- **Setup:** E1 plus `-v /var/run/docker.sock:/var/run/docker.sock:ro`.
- **Naming:** Docker metadata switched on by itself. `service_name` became the Compose service, `instance` became `<service>.<container>`, and `target_info` gained `container_name`.
- **Alloy quirks:** an `attributes` block requires `kubernetes { enable = "false" }`. `application_red` is not valid in v1.20.1.

### E3: Alloy, + SYS_ADMIN

- **Setup:** E2 plus `--cap-add=SYS_ADMIN`, with this `beyla.ebpf` block. It is reconstructed from the run notes; the Alloy `prometheus.*` plumbing is omitted.

```alloy
beyla.ebpf "spike" {
  discovery {
    instrument { open_ports = "1-65535" containers_only = true }
  }
  attributes {
    kubernetes { enable = "false" }
    select {
      attr = "beyla_network_flow_bytes"
      include = ["src.name", "dst.name", "transport"]
    }
    select {
      attr = "http_client_request_duration"
      include = ["service.name", "server.address", "server.port", "http.response.status_code", "http.request.method"]
    }
  }
  metrics { features = ["application", "application_service_graph", "network"] }
}
```

- **Errors:** none in the logs.
- **TLS and Go clients:** now instrumented.
- **External edges:** listed in [Which edges are visible](#which-edges-are-visible).

### E4: features env, 15 min

- **Setup:** E3 plus `OTEL_EBPF_METRICS_FEATURES=application,application_service_graph,network`.
- **Service graph:** still zero series.
- **Sonarr:** after 15 minutes, the client series included `sonarr → outgoing:443` with 5 requests. Sonarr's `NZBHYDRA_URL` and `IDP_URL` are both HTTPS, so these requests are nzbhydra2 or IdP calls, and the series can't say which.

### E5: standalone Beyla

- **Setup:** `grafana/beyla:3.35.0 --config=/beyla.yml` with the config below.
- **Result:** 1,755 series and zero `traces_service_graph_*`. The gap is in Beyla, not in Alloy.
- **`features: ["*"]` variant:** exits within a second with `setupStatsO11y: ... failed tracepoint attachment sock/inet_sock_set_state: neither debugfs nor tracefs are mounted`.

```yaml
discovery:
  instrument:
    - open_ports: "1-65535"
      containers_only: true
metrics:
  features: ["application", "application_service_graph"]
prometheus_export:
  port: 12399
  features: ["application", "application_service_graph"]
```

### E6: Baseline-compat

- **Setup:** E5 config with `features: ["application"]`, `--security-opt no-new-privileges`, default bridge network, `SYS_ADMIN`, and the socket.
- **Series:** after 2.5 minutes, 14,957 series, including 171 `http_client_request_duration_seconds_count` series.
- **Edges:** `radarr → sabnzbd:8080` and `sonarr → sabnzbd:8080`, both with `service_name` and `container_name` set.
- **Warnings:** 8, none fatal: no bpffs pin path, a cloud-metadata timeout, and generic Go instrumentation.
- **Teardown:** `docker stop -t 10` failed with "PID … is zombie and can not be killed". The container exited about 9.5 minutes later.
