#!/usr/bin/env python3
"""Download background images from umamusu.wiki categories."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from umamusume_web_crawler.web.umamusu_wiki import (
    download_umamusu_category_images,
    list_umamusu_category_files,
)


DEFAULT_CATEGORY = "Category:Game_Backgrounds"
DEFAULT_OUTPUT_DIR = Path("backgrounds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download umamusu.wiki background images into a local directory."
    )
    parser.add_argument(
        "category",
        nargs="?",
        default=DEFAULT_CATEGORY,
        help=f"umamusu.wiki category title or URL. Default: {DEFAULT_CATEGORY}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for downloaded files. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Manifest JSON path. Default: <output-dir>/download_manifest.json",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Limit files for a test run.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=500,
        help="MediaWiki category page size, max 500. Default: 500",
    )
    parser.add_argument(
        "--delay-s",
        type=float,
        default=0.5,
        help="Delay between API/download requests. Default: 0.5",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds. Default: 30",
    )
    parser.add_argument(
        "--endpoint",
        default="https://umamusu.wiki/w/api.php",
        help="MediaWiki API endpoint.",
    )
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--use-proxy",
        action="store_true",
        help="Force crawler proxy behavior on.",
    )
    proxy_group.add_argument(
        "--no-proxy",
        action="store_true",
        help="Force crawler proxy behavior off.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List matched file titles without downloading.",
    )
    return parser.parse_args()


def proxy_option(args: argparse.Namespace) -> bool | None:
    if args.use_proxy:
        return True
    if args.no_proxy:
        return False
    return None


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def main_async() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "download_manifest.json"
    use_proxy = proxy_option(args)

    if args.list_only:
        titles = await list_umamusu_category_files(
            args.category,
            endpoint=args.endpoint,
            page_limit=args.page_limit,
            max_files=args.max_files,
            timeout_s=args.timeout_s,
            use_proxy=use_proxy,
            delay_s=args.delay_s,
        )
        for title in titles:
            print(title)
        print(f"Matched {len(titles)} files.")
        return 0

    downloads = await download_umamusu_category_images(
        args.category,
        output_dir=output_dir,
        endpoint=args.endpoint,
        page_limit=args.page_limit,
        max_files=args.max_files,
        timeout_s=args.timeout_s,
        use_proxy=use_proxy,
        delay_s=args.delay_s,
    )
    manifest = {
        "category": args.category,
        "output_dir": str(output_dir),
        "count": len(downloads),
        "downloads": downloads,
    }
    write_manifest(manifest_path, manifest)

    total_bytes = sum(int(item.get("bytes", 0)) for item in downloads)
    print(f"Downloaded {len(downloads)} files to {output_dir}.")
    print(f"Total bytes: {total_bytes}")
    print(f"Manifest: {manifest_path}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
