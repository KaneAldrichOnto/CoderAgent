#!/usr/bin/env python3
"""
doc_tools.py - Generic documentation toolkit for CoderAgent.

App-agnostic helpers an agent (or a human) can shell out to from inside
the loop. None of this is required to use CoderAgent; install the optional
deps below only if you actually need image manipulation or diagram
rendering.

Subcommands
-----------

    screenshot      Capture a screenshot of the configured GUI app
                    (delegates to gui_nav.py screenshot).
    crop-image      Crop an image to pixel coordinates.
    annotate-rect   Draw a rectangle on an image.
    annotate-arrow  Draw an arrow on an image.
    annotate-label  Draw a text label on an image.
    render-diagram  Render a Mermaid `.mmd` source file to PNG via mmdc.
    view-image      Print dimensions / format of an image (sanity check).
    capture         Composite: click + screenshot + save.

Optional dependencies
---------------------

    pip install pillow                   # all image subcommands
    npm install -g @mermaid-js/mermaid-cli   # render-diagram

`gui_nav.py` (and its own optional `pywinauto` / `pillow` deps) is needed
only for `screenshot` and `capture`. Everything else degrades cleanly to
a single error message when its dep is missing.

Usage
-----

    python doc_tools.py --help
    python doc_tools.py crop-image src.png out.png 100 100 400 400
    python doc_tools.py annotate-rect src.png out.png 50 50 200 100 --color red
    python doc_tools.py render-diagram diagram.mmd diagram.png
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional dependencies — degrade gracefully
# ---------------------------------------------------------------------------
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


_PIL_HINT = (
    "Pillow is required for this command. Install with: pip install pillow"
)


# ---------------------------------------------------------------------------
# Color map shared by annotation subcommands
# ---------------------------------------------------------------------------
COLOR_MAP = {
    "red": (255, 0, 0),
    "green": (0, 180, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 140, 0),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "magenta": (255, 0, 255),
    "cyan": (0, 200, 200),
}


def _resolve_color(name: str) -> tuple[int, int, int]:
    return COLOR_MAP.get(name.lower(), (255, 0, 0))


def _ensure_png_suffix(p: str) -> str:
    return p if p.lower().endswith(".png") else p + ".png"


# ---------------------------------------------------------------------------
# Image subcommands
# ---------------------------------------------------------------------------

def cmd_crop_image(args) -> int:
    if not PIL_AVAILABLE:
        print(_PIL_HINT, file=sys.stderr)
        return 1
    src = Path(args.source)
    dest = Path(_ensure_png_suffix(args.dest))
    if not src.exists():
        print(f"ERROR: source image not found: {src}", file=sys.stderr)
        return 1
    try:
        img = PILImage.open(str(src))
        w, h = img.size
        left = max(0, min(args.left, w))
        top = max(0, min(args.top, h))
        right = max(left + 1, min(args.right, w))
        bottom = max(top + 1, min(args.bottom, h))
        cropped = img.crop((left, top, right, bottom))
        dest.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(str(dest), format="PNG")
        cw, ch = cropped.size
        print(f"Cropped ({left},{top})-({right},{bottom}) from {src} "
              f"-> {dest} ({cw}x{ch}px)")
        return 0
    except Exception as e:
        print(f"ERROR cropping image: {e}", file=sys.stderr)
        return 1


def cmd_annotate_rect(args) -> int:
    if not PIL_AVAILABLE:
        print(_PIL_HINT, file=sys.stderr)
        return 1
    from PIL import ImageDraw
    src = Path(args.source)
    dest = Path(_ensure_png_suffix(args.dest))
    if not src.exists():
        print(f"ERROR: source image not found: {src}", file=sys.stderr)
        return 1
    try:
        img = PILImage.open(str(src)).convert("RGBA")
        overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        rgb = _resolve_color(args.color)
        draw.rectangle([args.left, args.top, args.right, args.bottom],
                       outline=rgb, width=args.width)
        out = PILImage.alpha_composite(img, overlay).convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest), format="PNG")
        print(f"Drew {args.color} rectangle "
              f"({args.left},{args.top})-({args.right},{args.bottom}) "
              f"on {src} -> {dest}")
        return 0
    except Exception as e:
        print(f"ERROR annotating image: {e}", file=sys.stderr)
        return 1


def cmd_annotate_arrow(args) -> int:
    if not PIL_AVAILABLE:
        print(_PIL_HINT, file=sys.stderr)
        return 1
    from PIL import ImageDraw
    src = Path(args.source)
    dest = Path(_ensure_png_suffix(args.dest))
    if not src.exists():
        print(f"ERROR: source image not found: {src}", file=sys.stderr)
        return 1
    try:
        img = PILImage.open(str(src)).convert("RGBA")
        overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        rgb = _resolve_color(args.color)

        fx, fy = args.from_x, args.from_y
        tx, ty = args.to_x, args.to_y
        draw.line([(fx, fy), (tx, ty)], fill=rgb, width=args.width)

        # Arrowhead at (tx, ty)
        angle = math.atan2(ty - fy, tx - fx)
        head_len = max(15, args.width * 5)
        head_angle = math.radians(25)
        lx = tx - head_len * math.cos(angle - head_angle)
        ly = ty - head_len * math.sin(angle - head_angle)
        rx = tx - head_len * math.cos(angle + head_angle)
        ry = ty - head_len * math.sin(angle + head_angle)
        draw.polygon([(tx, ty), (int(lx), int(ly)), (int(rx), int(ry))],
                     fill=rgb)

        out = PILImage.alpha_composite(img, overlay).convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest), format="PNG")
        print(f"Drew {args.color} arrow ({fx},{fy})->({tx},{ty}) "
              f"on {src} -> {dest}")
        return 0
    except Exception as e:
        print(f"ERROR annotating image: {e}", file=sys.stderr)
        return 1


def cmd_annotate_label(args) -> int:
    if not PIL_AVAILABLE:
        print(_PIL_HINT, file=sys.stderr)
        return 1
    from PIL import ImageDraw, ImageFont
    src = Path(args.source)
    dest = Path(_ensure_png_suffix(args.dest))
    if not src.exists():
        print(f"ERROR: source image not found: {src}", file=sys.stderr)
        return 1
    try:
        img = PILImage.open(str(src)).convert("RGBA")
        overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        fg = _resolve_color(args.color)

        try:
            font = ImageFont.truetype("arial.ttf", args.size)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype(
                    "DejaVuSans.ttf", args.size)
            except (OSError, IOError):
                font = ImageFont.load_default()

        bbox = draw.textbbox((args.x, args.y), args.text, font=font)
        if args.background.lower() != "none":
            bg = _resolve_color(args.background)
            pad = 3
            draw.rectangle(
                [bbox[0] - pad, bbox[1] - pad,
                 bbox[2] + pad, bbox[3] + pad],
                fill=bg + (230,),
            )
        draw.text((args.x, args.y), args.text, fill=fg, font=font)

        out = PILImage.alpha_composite(img, overlay).convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest), format="PNG")
        print(f"Drew {args.color} label '{args.text}' at ({args.x},{args.y}) "
              f"on {src} -> {dest}")
        return 0
    except Exception as e:
        print(f"ERROR annotating image: {e}", file=sys.stderr)
        return 1


def cmd_view_image(args) -> int:
    if not PIL_AVAILABLE:
        # Fall back to a basic stat so the agent at least knows the file exists
        p = Path(args.path)
        if not p.exists():
            print(f"ERROR: image not found: {p}", file=sys.stderr)
            return 1
        sz = p.stat().st_size
        print(f"Path: {p}\nSize: {sz} bytes\nExtension: {p.suffix}\n"
              f"(Pillow not installed -- cannot read pixel dimensions)")
        return 0
    p = Path(args.path)
    if not p.exists():
        print(f"ERROR: image not found: {p}", file=sys.stderr)
        return 1
    try:
        sz = p.stat().st_size
        sz_str = f"{sz/1024:.1f}KB" if sz < 1_000_000 else f"{sz/1_000_000:.2f}MB"
        with PILImage.open(str(p)) as im:
            print(f"Path:       {p}")
            print(f"Size:       {sz_str}")
            print(f"Extension:  {p.suffix}")
            print(f"Format:     {im.format}")
            print(f"Mode:       {im.mode}")
            print(f"Dimensions: {im.size[0]}x{im.size[1]}px")
        return 0
    except Exception as e:
        print(f"ERROR reading image: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------

def _find_mmdc() -> str:
    """Locate the mmdc CLI; return the path or '' if not found."""
    found = shutil.which("mmdc")
    if found:
        return found
    # On Windows the global npm bin may not be on PATH in this process
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for name in ("mmdc.cmd", "mmdc.ps1", "mmdc"):
            cand = Path(appdata) / "npm" / name
            if cand.exists():
                return str(cand)
    return ""


def cmd_render_diagram(args) -> int:
    src = Path(args.source)
    if not src.exists():
        print(f"ERROR: mermaid source not found: {src}", file=sys.stderr)
        return 1
    dest = Path(_ensure_png_suffix(args.dest))

    mmdc = _find_mmdc()
    if not mmdc:
        print("ERROR: mmdc (Mermaid CLI) not found on PATH.\n"
              "Install with: npm install -g @mermaid-js/mermaid-cli",
              file=sys.stderr)
        return 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        mmdc,
        "-i", str(src),
        "-o", str(dest),
        "-b", args.background,
        "-w", str(args.width),
        "-t", args.theme,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("ERROR: mmdc timed out after 60 seconds.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR running mmdc: {e}", file=sys.stderr)
        return 1
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip()
               or "(no output from mmdc)")
        print(f"ERROR rendering diagram: {err}", file=sys.stderr)
        return 1
    if not dest.exists():
        print("ERROR: mmdc completed but output file was not created.",
              file=sys.stderr)
        return 1

    if PIL_AVAILABLE:
        try:
            with PILImage.open(str(dest)) as im:
                w, h = im.size
            print(f"Rendered {src} -> {dest} ({w}x{h}px)")
            return 0
        except Exception:
            pass
    print(f"Rendered {src} -> {dest}")
    return 0


# ---------------------------------------------------------------------------
# gui_nav.py wrappers (screenshot, capture)
# ---------------------------------------------------------------------------

def _import_gui_nav():
    """Import gui_nav from the same directory; return module or None."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import gui_nav  # type: ignore
        return gui_nav
    except ImportError as e:
        print(f"ERROR: cannot import gui_nav.py from this directory: {e}",
              file=sys.stderr)
        return None


def cmd_screenshot(args) -> int:
    """Capture a screenshot of the configured GUI app and save it."""
    gn = _import_gui_nav()
    if gn is None:
        return 1
    save_path = Path(args.name)
    if not save_path.suffix:
        save_path = save_path.with_suffix(".png")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = gn.nav_screenshot(str(save_path))
    except Exception as e:
        print(f"ERROR: screenshot failed: {e}", file=sys.stderr)
        return 1
    print(result)
    if args.desc:
        meta = save_path.with_suffix(save_path.suffix + ".meta.txt")
        meta.write_text(
            f"Captured: {datetime.now().isoformat()}\n"
            f"Description: {args.desc}\n",
            encoding="utf-8",
        )
        print(f"  -> wrote sidecar {meta}")
    return 0 if not result.lower().startswith("error") else 1


def cmd_capture(args) -> int:
    """Composite: click (or right-click) a control, then screenshot + save.

    The screenshot is taken AFTER the click so the model sees the
    resulting state (a dialog opening, a panel switching, etc.).
    """
    gn = _import_gui_nav()
    if gn is None:
        return 1
    save_path = Path(args.save_as)
    if not save_path.suffix:
        save_path = save_path.with_suffix(".png")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.right_click:
            click_result = gn.nav_right_click(args.name)
        else:
            click_result = gn.nav_click(args.name)
    except Exception as e:
        print(f"ERROR: click failed: {e}", file=sys.stderr)
        return 1
    print(click_result)
    try:
        ss_result = gn.nav_screenshot(str(save_path))
    except Exception as e:
        print(f"ERROR: screenshot after click failed: {e}", file=sys.stderr)
        return 1
    print(ss_result)
    return 0 if not ss_result.lower().startswith("error") else 1


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc_tools",
        description=__doc__.split("\n\n")[0] if __doc__ else "doc_tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_ss = sub.add_parser("screenshot",
                          help="Capture a screenshot of the configured GUI app")
    p_ss.add_argument("name", help="Output filename (relative or absolute; "
                                   ".png appended if missing)")
    p_ss.add_argument("--desc", default="",
                      help="Optional description; saved alongside as "
                           "<name>.png.meta.txt")
    p_ss.set_defaults(func=cmd_screenshot)

    p_crop = sub.add_parser("crop-image", help="Crop an image to pixel coords")
    p_crop.add_argument("source")
    p_crop.add_argument("dest")
    p_crop.add_argument("left", type=int)
    p_crop.add_argument("top", type=int)
    p_crop.add_argument("right", type=int)
    p_crop.add_argument("bottom", type=int)
    p_crop.set_defaults(func=cmd_crop_image)

    p_rect = sub.add_parser("annotate-rect",
                            help="Draw a rectangle on an image")
    p_rect.add_argument("source")
    p_rect.add_argument("dest")
    p_rect.add_argument("left", type=int)
    p_rect.add_argument("top", type=int)
    p_rect.add_argument("right", type=int)
    p_rect.add_argument("bottom", type=int)
    p_rect.add_argument("--color", default="red")
    p_rect.add_argument("--width", type=int, default=3)
    p_rect.set_defaults(func=cmd_annotate_rect)

    p_arr = sub.add_parser("annotate-arrow",
                           help="Draw an arrow from (fX,fY) to (tX,tY)")
    p_arr.add_argument("source")
    p_arr.add_argument("dest")
    p_arr.add_argument("from_x", type=int, metavar="fX")
    p_arr.add_argument("from_y", type=int, metavar="fY")
    p_arr.add_argument("to_x", type=int, metavar="tX")
    p_arr.add_argument("to_y", type=int, metavar="tY")
    p_arr.add_argument("--color", default="red")
    p_arr.add_argument("--width", type=int, default=3)
    p_arr.set_defaults(func=cmd_annotate_arrow)

    p_lbl = sub.add_parser("annotate-label",
                           help="Draw a text label on an image")
    p_lbl.add_argument("source")
    p_lbl.add_argument("dest")
    p_lbl.add_argument("x", type=int, metavar="X")
    p_lbl.add_argument("y", type=int, metavar="Y")
    p_lbl.add_argument("text")
    p_lbl.add_argument("--color", default="red")
    p_lbl.add_argument("--size", type=int, default=18)
    p_lbl.add_argument("--background", default="white",
                       help="Background color, or 'none' to disable")
    p_lbl.set_defaults(func=cmd_annotate_label)

    p_rd = sub.add_parser("render-diagram",
                          help="Render a Mermaid `.mmd` source file to PNG")
    p_rd.add_argument("source", help="Path to .mmd file")
    p_rd.add_argument("dest", help="Output .png path")
    p_rd.add_argument("--width", type=int, default=1024)
    p_rd.add_argument("--theme", default="default",
                      choices=["default", "dark", "forest", "neutral"])
    p_rd.add_argument("--background", default="white")
    p_rd.set_defaults(func=cmd_render_diagram)

    p_vi = sub.add_parser("view-image",
                          help="Print dimensions / format of an image")
    p_vi.add_argument("path")
    p_vi.set_defaults(func=cmd_view_image)

    p_cap = sub.add_parser("capture",
                           help="Click a control, then screenshot and save "
                                "(composite shortcut)")
    p_cap.add_argument("name", help="UIA name of the control to click")
    p_cap.add_argument("-s", "--save-as", required=True,
                       help="Output screenshot filename")
    p_cap.add_argument("--right-click", action="store_true",
                       help="Right-click instead of left-click")
    p_cap.set_defaults(func=cmd_capture)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
