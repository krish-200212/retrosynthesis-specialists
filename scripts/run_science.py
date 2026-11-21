"""Run with an existing managed Python containing MCP; request JSON on stdin.

No jobs run on import. An explicitly supplied private run directory is durable:
repeated submit refuses and observe/status never submit. Remote credentials stay
in the configured Sci profile and are handled by the existing public server.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retro_specialists.science import MCPTransport, ScienceError, run_job


async def execute(args):
    config = json.loads(args.config.read_text())
    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise ScienceError("Request too large")
    request = json.loads(raw)
    async with MCPTransport(config, args.run_dir / "transport", timeout_seconds=args.timeout) as transport:
        return await run_job(transport.call, request, args.run_dir, action=args.action,
            doc_id=args.doc_id, timeout_seconds=args.timeout, download=not args.no_download)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--action", choices=("submit", "observe", "status"), required=True)
    parser.add_argument("--doc-id")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result["state"] == "completed" else 2
    except Exception as exc:
        print(json.dumps({"state": "local_error", "reason": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
