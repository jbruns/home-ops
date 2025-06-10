#!/bin/bash
CATEGORY="$1"
SOURCE_DIR="$2"

if [ "$CATEGORY" = "abr-prowlarr" ]; then
    cp -r "$SOURCE_DIR" /data/audiobooks/
fi
