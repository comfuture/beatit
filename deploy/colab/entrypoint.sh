#!/bin/sh
set -eu
umask 077
if [ -n "${COLAB_SESSION_SEED:-}" ]; then
    mkdir -p /tmp/colab-state
    cp "$COLAB_SESSION_SEED" /tmp/colab-state/sessions.json
    chmod 600 /tmp/colab-state/sessions.json
fi
exec colab "$@"
