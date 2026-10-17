# One Grafana spans hosts; each host keeps its own telemetry

Some Dependencies run on other machines that have their own Prometheus and Loki. Instead of shipping their data here (remote-write or federation) or scraping them from the Host, the Host's Grafana adds each machine's Prometheus and Loki as extra datasources and queries them in place. Grafana is the only shared piece: each machine keeps its own collection, storage, and alerting, so no machine's deployment depends on another's, and another machine can be added later by adding a datasource. Each datasource's address and credentials are SOPS-encrypted. A remote machine's telemetry dies with that machine, so the Host also Probes each External Dependency itself and does not rely on the remote data for Uptime.

## Considered Options

- **Remote-write or federation into the Host's Prometheus**: rejected; it puts all the data in one store, but it couples each machine's deployment to the Host's.
- **The Host's Prometheus scrapes the remote exporters**: rejected; it needs a second scraper, plus exposure on the LAN or firewall.
