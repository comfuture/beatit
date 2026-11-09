#!/usr/bin/env python3
"""Certbot HTTP-01 hook for bootstrap through Tunnel and subsequent direct HTTPS.

The challenge is public by design. Persist it in the Nginx webroot, and during
the initial proxied issuance also copy it into the current application shell.
No application secrets or Cloudflare DNS credentials are needed.
"""

import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    token = os.environ["CERTBOT_TOKEN"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("Unexpected ACME token")
    cleanup = len(sys.argv) > 1 and sys.argv[1] == "cleanup"
    challenge = Path("/var/lib/beatit-nginx/acme/.well-known/acme-challenge") / token
    if cleanup:
        challenge.unlink(missing_ok=True)
    else:
        challenge.parent.mkdir(parents=True, exist_ok=True)
        challenge.write_text(os.environ["CERTBOT_VALIDATION"])
        challenge.chmod(0o644)
    # The current Cloudflare Tunnel routes to the app until DNS is switched.
    code = """import os,re,sys
from transcription.app import STATIC
token=sys.argv[1]
assert re.fullmatch(r'[A-Za-z0-9_-]+',token)
path=STATIC/'.well-known'/'acme-challenge'/token
if sys.argv[2]=='cleanup': path.unlink(missing_ok=True)
else:
 path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text(sys.stdin.read())
 path.chmod(0o644)
"""
    subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "0",
            "-i",
            "beatit-multiuser-beatit-1",
            "python",
            "-c",
            code,
            token,
            "cleanup" if cleanup else "auth",
        ],
        input="" if cleanup else os.environ["CERTBOT_VALIDATION"],
        text=True,
        # Once DNS is direct, Nginx serves the persistent webroot even while the
        # app is restarting. ACME itself verifies the public challenge response.
        check=False,
    )


if __name__ == "__main__":
    main()
