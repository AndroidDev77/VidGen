from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from vidgen.storage.content_address import sha256_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a source video to a local VidGen API")
    parser.add_argument("video", type=Path)
    parser.add_argument("--api", default="http://localhost:8000/api/v1")
    parser.add_argument("--name", default="Local recap")
    parser.add_argument("--part-size", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args()
    digest, byte_size = sha256_file(args.video)
    with httpx.Client(base_url=args.api, timeout=120) as client:
        project_response = client.post(
            "/projects",
            json={
                "name": args.name,
                "target_duration_seconds": 300,
                "visual_style": "flat editorial cartoon",
                "humor_intensity": 5,
            },
        )
        project_response.raise_for_status()
        project = project_response.json()
        upload_response = client.post(
            f"/projects/{project['id']}/uploads",
            json={
                "filename": args.video.name,
                "media_type": "video/mp4",
                "expected_size": byte_size,
                "expected_sha256": digest,
                "part_size": args.part_size,
            },
        )
        upload_response.raise_for_status()
        upload = upload_response.json()
        with args.video.open("rb") as stream:
            part_number = 0
            while chunk := stream.read(args.part_size):
                response = client.put(f"/uploads/{upload['id']}/parts/{part_number}", content=chunk)
                response.raise_for_status()
                part_number += 1
        complete = client.post(f"/uploads/{upload['id']}/complete")
        complete.raise_for_status()
        print(json.dumps({"project": project, "upload": complete.json()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
