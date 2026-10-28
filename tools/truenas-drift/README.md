# truenas-drift

Reports every difference between the Host Configuration record ([`host/truenas/`](../../host/truenas/README.md))
and the Host (ADR 0015). Repo Tooling: the agent runs it after applying a record change, and it changes nothing.

It reads the record, decrypts `identifiers.sops.yaml` to turn roles into values, and reads the Host with
`hops truenas call --json`. Every listed setting is compared; unlisted settings are not compared with TrueNAS's
defaults. Lists are complete: an item on the Host that the record doesn't list is drift, except TrueNAS's
built-in users and groups and the media identities in `host/identities.tsv`.

## Run

```sh
uv run tools/truenas-drift/truenas_drift.py
uv run tools/truenas-drift/truenas_drift.py --unchecked
```

It prints nothing and exits 0 when the record and the Host match. Otherwise it prints one line per difference
and exits 1, or exits 2 when it can't read the record or the Host. Addresses, names and logins are printed
by role, never as values.

`--unchecked` also lists settings it could not compare: SMB share ACLs (`sharing.smb.getacl` is not a hops read),
values hops redacts (init/shutdown and cron commands, sudo commands, SSH password login groups, the UI
certificate), and the boot pool.

Each hops call logs in afresh and TrueNAS rate-limits logins, so a run takes a few minutes; rate-limited calls
are retried.

Needs `uv`, `sops` with the Operator Key, and `hops` configured for the Host.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/truenas-drift
```

The tests use a synthetic record and Host; they need no hops, SOPS or Host.
