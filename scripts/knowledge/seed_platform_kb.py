"""Prepare a platform knowledge refresh offline. This command never publishes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from app import platform_content
from app.db import PLATFORM_TENANT_ID


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-manifest',type=Path)
    args=parser.parse_args()
    current=json.loads(args.current_manifest.read_text(encoding='utf-8')) if args.current_manifest else None
    print(json.dumps(platform_content.refresh_plan(current,tenant_id=PLATFORM_TENANT_ID),indent=2))


if __name__=='__main__':
    main()
