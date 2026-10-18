# Each Service gets the minimal storage access its Roles need, one tree per Library

The goal is to limit what a fully compromised Service can destroy: SABnzbd must not be able to wipe the TV Library, Sonarr must not be able to delete Movies, and Plex and Emby must not be able to write to any Library. Unix ownership cannot carry this: the media tree is one dataset, an Organizer's import is a rename that keeps the Downloader's UID, and a non-root Organizer cannot `chown` a file to itself. So access is scoped by what each Service mounts, and the shared group plus setgid directories give access within it.

Each Library gets its own tree, `/media/<library>/{library,usenet,torrents}`, and its one Organizer mounts that whole tree as a single bind mount. Docker bind mounts are separate mount points, so a rename between two of them fails with `EXDEV`; the Organizer would fall back to copy-then-delete, and hardlinks for seeding would break. Downloaders mount only their download folders. Media Servers mount Libraries read-only, except a recordings Library they organize themselves. A Service holding several Roles gets the union of what those Roles need, and nothing more.

## Considered Options

- **One `/media` mount for every Organizer** (the TRaSH Guides layout): rejected; it gives every Organizer write access to every Library.
- **Separate bind mounts per folder**: rejected; every import becomes a full copy, and seeding hardlinks break.
- **Separate datasets per Library**: not needed; one tree per Library on the existing dataset gives the same isolation with renames only.
