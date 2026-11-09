"""Private S3-compatible object storage. Browser requests always pass the owner API."""

import hashlib
import os
from pathlib import Path

import boto3
from botocore.config import Config


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ObjectStorage:
    def __init__(self):
        required = (
            "BEATIT_S3_ENDPOINT",
            "BEATIT_S3_BUCKET",
            "BEATIT_S3_ACCESS_KEY_ID",
            "BEATIT_S3_SECRET_ACCESS_KEY",
        )
        if missing := [name for name in required if not os.environ.get(name)]:
            raise RuntimeError(
                "Private object storage configuration missing: " + ", ".join(missing)
            )
        endpoint = os.environ["BEATIT_S3_ENDPOINT"]
        if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1:"):
            raise RuntimeError("Remote object storage requires HTTPS.")
        self.bucket = os.environ["BEATIT_S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=os.environ.get("BEATIT_S3_REGION", "auto"),
            aws_access_key_id=os.environ["BEATIT_S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["BEATIT_S3_SECRET_ACCESS_KEY"],
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 2},
            ),
        )

    def put(self, key: str, path: Path):
        self.client.upload_file(str(path), self.bucket, key)

    def get(self, key: str, path: Path):
        self.client.download_file(self.bucket, key, str(path))

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def probe(self):
        self.client.head_bucket(Bucket=self.bucket)
