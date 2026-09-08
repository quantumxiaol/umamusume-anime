"""Create an isolated short render fixture; never modify production content."""
import json
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True, help="Existing content project name")
parser.add_argument("--duration-ms", type=int, default=13000)
args = parser.parse_args()
if Path(args.source).name != args.source or args.source == "StaticReuseCheck_4k60" or args.duration_ms <= 0:
    parser.error("Use an existing source project and positive duration")
root = Path(__file__).resolve().parents[1]
source = root / "my-video/public/content" / args.source
target = root / "my-video/public/content/StaticReuseCheck_4k60"
target.mkdir(exist_ok=True)
for folder in ("images", "audio"):
    link = target / folder
    if link.exists() and link.resolve() != (source / folder).resolve():
        raise SystemExit(f"Fixture already references another source: {link}")
    if not link.exists():
        link.symlink_to(source / folder, target_is_directory=True)
data = json.loads((source / "timeline.json").read_text())
for key in ("elements", "text", "audio"):
    data[key] = [dict(item, endMs=min(item["endMs"], args.duration_ms))
                 for item in data[key] if item["startMs"] < args.duration_ms]
(target / "timeline.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(target)
