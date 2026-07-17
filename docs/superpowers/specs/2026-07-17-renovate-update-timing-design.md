# Renovate Update Timing Design

## Goal

Prevent Renovate updates from remaining indefinitely under "Pending Status Checks" while retaining the five-day release cooldown whenever a datasource supplies a release timestamp.

## Design

- Set `minimumReleaseAgeBehaviour` to `timestamp-optional`. Releases with timestamps must still be at least five days old; releases from sources such as GHCR that lack supported timestamps may proceed.
- Include GitHub's `published_at` value as `releaseTimestamp` in the Portainer custom datasource transform so its LTS releases continue to honor the five-day cooldown.
- Replace the deprecated one-hour Later schedule with the Cron schedule `* 10-13 * * 5`, allowing branch creation from 10:00 through 13:59 Pacific on Fridays. This meets Renovate's recommendation for a schedule window of at least three to four hours.

## Validation

Validate the configuration against Renovate's published JSON schema and confirm the JSONata transform returns Portainer releases with `version` and `releaseTimestamp`.
