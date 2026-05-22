"""
Package the extension/ folder into a .zip that Octo Browser can import.

Usage:
    python build_extension.py

Output:
    dist/octo_probe_<version>.zip
"""
import json
import os
import zipfile

SRC_DIR = os.path.join(os.path.dirname(__file__), "extension")
OUT_DIR = os.path.join(os.path.dirname(__file__), "dist")

# Files to exclude from the package
EXCLUDE = {"gen_icons.py", "__pycache__"}


def build():
    # Read version from manifest
    with open(os.path.join(SRC_DIR, "manifest.json")) as f:
        version = json.load(f).get("version", "0.0.0")

    out_file = os.path.join(OUT_DIR, f"octo_probe_{version}.zip")

    # Ensure icons exist
    icons_dir = os.path.join(SRC_DIR, "icons")
    missing = [f"icon{sz}.png" for sz in (16, 48, 128)
               if not os.path.exists(os.path.join(icons_dir, f"icon{sz}.png"))]
    if missing:
        print("Icons missing — generating them first...")
        import subprocess, sys
        subprocess.check_call([sys.executable, os.path.join(SRC_DIR, "gen_icons.py")])

    os.makedirs(OUT_DIR, exist_ok=True)

    if os.path.exists(out_file):
        os.remove(out_file)

    with zipfile.ZipFile(out_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SRC_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            for fname in files:
                if fname in EXCLUDE or fname.endswith(".pyc"):
                    continue
                abs_path = os.path.join(root, fname)
                arc_name = os.path.relpath(abs_path, SRC_DIR)
                zf.write(abs_path, arc_name)
                print(f"  + {arc_name}")

    size_kb = os.path.getsize(out_file) / 1024
    print(f"\nBuilt: {out_file}  ({size_kb:.1f} KB)")
    print("\nTo add to an Octo profile:")
    print("  1. Open Octo Browser > Profiles > Edit profile")
    print("  2. Go to Extensions tab > Import from file")
    print(f"  3. Select: {out_file}")


if __name__ == "__main__":
    build()
