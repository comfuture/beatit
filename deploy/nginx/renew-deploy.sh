#!/bin/sh
set -eu
if systemctl is-active --quiet beatit-nginx.service; then
    systemctl reload beatit-nginx.service
fi
