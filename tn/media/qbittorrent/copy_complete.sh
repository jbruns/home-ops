#!/bin/bash
CATEGORY="$1"
SOURCE_DIR="$2"
DEST_DIR="$3"

if [ "$CATEGORY" = "abr-prowlarr" ]; then
    cp -r "$SOURCE_DIR" "$DEST_DIR"
fi
