# Backups go straight to the Host's own B2 bucket

stack-back wrote its restic repository to a local dataset, and a TrueNAS Cloud Sync task copied that dataset, in SYNC mode, to a folder of a B2 bucket. The sibling deployment repos instead have restic write straight to the root of each Host's own bucket. Our layout differed from theirs and had weaknesses of its own. It needed a second scheduled job that could fail on its own. SYNC copied any corruption or deletion in the local repository offsite. The bucket also kept files that had nothing to do with restic, such as the retired offen tarballs and their hidden versions. We decided that **stack-back writes straight to the root of the Host's own B2 bucket, over B2's S3-compatible API**. The bucket was wiped and the repository re-initialised with a rotated password, so history starts over (#703).

- **Lifecycle:** every Host's bucket keeps "Keep only the last version of the file". restic's S3 backend only hides the files it prunes, and this rule deletes them a day later, as restic's documentation recommends.
- **Keys:** stack-back holds the only key that can write or delete, scoped to the bucket. Anything else that reads the repository (restic-exporter, backup-coverage) shares a read-only key, and keeps a restic cache volume so its hourly reads stay small.
- **Password:** the restic password is the only way to read a Backup, so the Operator's password manager holds a copy as well as the encrypted env file.

## Considered Options

- **Keep the local repository and point Cloud Sync at the bucket root**: rejected. It is the smallest change and keeps restores local, but it keeps the second job and copies local damage offsite.
- **Write to both B2 and a local repository (`restic copy`)**: rejected. It costs another job and a second repository to monitor, only to make restores faster.
- **Keep hidden versions for 30 days as an undo window**: rejected. The same key that prunes can delete versions, so it protects against nothing a compromised Host could do, and costs storage for pruned data.

## Consequences

- Restores, `restic check` and the exporters' reads go over the internet. At about 490 GiB, storage costs a few dollars a month, and the exporters' reads fit within B2's free daily calls.
- A Host that is compromised can delete its own Backups, because stack-back's key can delete files. This is an accepted risk.
