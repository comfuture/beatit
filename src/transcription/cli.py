import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Local drum transcription")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the local web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--data-dir", type=Path, default=None)
    sub.add_parser("doctor", help="Report available inference devices and tools")
    warmup = sub.add_parser("download-model", help="Download model checkpoints before processing")
    warmup.add_argument(
        "--model", default="all", choices=["all", "htdemucs", "drumsep", "beat-this"]
    )
    args = parser.parse_args()
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)
    elif args.command == "doctor":
        import json

        from .runtime import capabilities

        print(json.dumps(capabilities(), indent=2, ensure_ascii=False))
    else:
        if args.model in ("all", "htdemucs"):
            from demucs.pretrained import get_model

            print(f"Demucs htdemucs: {get_model('htdemucs').sources}")
        if args.model in ("all", "drumsep"):
            from .drumsep import checkpoint_path

            print(f"DrumSep: {checkpoint_path()}")
        if args.model in ("all", "beat-this"):
            from .rhythm import beat_this_checkpoint

            print(f"Beat This!: {beat_this_checkpoint()}")
