# Photo Collections live in Immich, one owner per item

The household's photos lived in one Nextcloud group folder that everyone owned jointly, filled by the Nextcloud phone apps' auto-upload, with only the stock Photos app on top. We moved photo hosting to Immich, a Critical Stack of its own (ADR 0004), and Nextcloud no longer holds photos. Immich holds the originals; it does not index Nextcloud's files as an External Library, which would couple two Critical Stacks and leave Nextcloud doing the hosting. Immich gives every item exactly one owner, so each household member has a Photo Collection, and members see each other's through partner sharing and shared albums. Items with no clear owner go to the Operator's account. For iPhone photos, the source of truth is the phones' iCloud library, backed up by the Immich mobile app, rather than the Nextcloud copies, whose re-encoding would have created duplicates. The migration imports a Nextcloud copy only when no matching original is found.

## Considered Options

- **Immich reads Nextcloud's folder as an External Library**: rejected; it couples two Critical Stacks, and phones would keep uploading to Nextcloud.
- **A shared household account that owns the archive**: rejected; it would stand in for joint ownership, which Immich does not model, and add an account whose phone backs up nothing.
- **Import every Nextcloud file, phone folders included**: rejected; the Nextcloud iOS app's copies do not match the iCloud originals by checksum, so the same Live Photos would land twice.
