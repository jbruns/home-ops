# Sent on stdin to `sh -s -- <folder>` in a container of Immich's server image, whose exiftool it runs.
# Prints exiftool's JSON for every file under <folder>, the input of `photo_migrate.py plan`.
set -eu
exiftool=$(find /usr/src/app/server/node_modules/.pnpm -path '*exiftool-vendored.pl@*/bin/exiftool' | head -1)
exec perl "$exiftool" -r -j -G1 -a -n -q -q -ext '*' \
  -Make -Model -DateTimeOriginal -CreationDate -CreateDate -MediaCreateDate \
  -MIMEType -FileModifyDate -FileSize -Error "$1"
