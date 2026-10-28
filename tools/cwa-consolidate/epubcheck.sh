#!/bin/sh
# Runs inside a throwaway JRE container (see README.md): stdin is a gzipped tar of epubcheck's release
# folder and epubs.txt (library-relative EPUB paths); stdout is one JSON line per EPUB.
set -eu
work=$(mktemp -d)
tar -xzf - -C "$work"
jar=$(ls "$work"/epubcheck-*/epubcheck.jar)
while IFS= read -r p; do
  [ -n "$p" ] || continue
  summary=$(java -jar "$jar" "/calibre-library/$p" 2>&1 | grep -E '^Messages:' || true)
  fatal=$(printf '%s' "$summary" | sed -n 's/.* \([0-9][0-9]*\) fatals\{0,1\}.*/\1/p')
  errors=$(printf '%s' "$summary" | sed -n 's/.* \([0-9][0-9]*\) errors\{0,1\}.*/\1/p')
  esc=$(printf '%s' "$p" | sed 's/\\/\\\\/g; s/"/\\"/g')
  # No summary means epubcheck could not open the file: count it as fatal
  printf '{"path": "%s", "fatal": %s, "errors": %s}\n' "$esc" "${fatal:-1}" "${errors:-0}"
done < "$work/epubs.txt"
