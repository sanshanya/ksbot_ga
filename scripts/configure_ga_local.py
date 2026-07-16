"""Generate local GenericAgent config files without overwriting existing state."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GA_ROOT = Path(os.getenv("GA_ROOT", ROOT / "vendor" / "GenericAgent"))
MYKEY_TEMPLATE = ROOT / "examples" / "mykey_openai_compatible.py"
MYKEY_LOCAL_TEMPLATE = ROOT / "examples" / "mykey.local.example.py"
VISION_TEMPLATE = GA_ROOT / "memory" / "vision_api.template.py"
VISION_TARGET = GA_ROOT / "memory" / "vision_api.py"
MYKEY_TARGET = GA_ROOT / "mykey.py"


def copy_config(source: Path, target: Path, *, force: bool) -> None:
    if not source.is_file():
        raise SystemExit(f"template not found: {source}")
    if target.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing config: {target} (use --force)")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"Created {target} from {source}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local GA config files.")
    parser.add_argument("--mykey-only", action="store_true", help="skip vision setup")
    parser.add_argument("--vision-only", action="store_true", help="skip mykey setup")
    parser.add_argument("--force", action="store_true", help="overwrite existing local config")
    parser.add_argument(
        "--local",
        action="store_true",
        help="use the local GLM-5.2 example instead of the generic placeholder",
    )
    args = parser.parse_args()

    if not args.vision_only:
        source = MYKEY_LOCAL_TEMPLATE if args.local else MYKEY_TEMPLATE
        copy_config(source, MYKEY_TARGET, force=args.force)
        print("Edit mykey.py to set apikey, apibase, and model for your endpoint.")
    if not args.mykey_only:
        if VISION_TEMPLATE.is_file():
            copy_config(VISION_TEMPLATE, VISION_TARGET, force=args.force)
            print("Edit vision_api.py for the selected vision backend and model.")
        else:
            print(f"Vision template not found at {VISION_TEMPLATE}, skipping vision.")

    print("\nThese files are git-ignored and will not be committed.")
    print("Install vision dependencies when needed: uv sync --extra vision")


if __name__ == "__main__":
    main()
