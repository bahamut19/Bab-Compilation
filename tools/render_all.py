#!/usr/bin/env python3
"""Render the HTML site and every DOCX edition, then put the DOCX files in the site's downloads/ folder.

    python tools/render_all.py            # everything
    python tools/render_all.py --docx-only
    python tools/render_all.py --web-only
    python tools/render_all.py --web-only --memory-gb 16    # if Quarto runs out of memory
    python tools/render_all.py --web-only --no-search       # builds the site without the search index

Word files stay PRIVATE in _docx/. Only PDFs that you save from Word into the project's pdf/ folder
are copied to _book/downloads/ (the public site). Any .docx found in _book/downloads is removed.

Then commit and push; Netlify publishes the _book folder.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)


MEMORY_GB = 0


def quarto(*args):
    exe = shutil.which("quarto")
    if not exe:
        sys.exit("Quarto was not found on PATH. Install it from https://quarto.org and reopen the terminal.")
    env = os.environ.copy()
    if MEMORY_GB:
        mb = MEMORY_GB * 1024
        env["QUARTO_DENO_V8_OPTIONS"] = f"--stack-size=8192,--max-old-space-size={mb}"
    print("\n>> quarto", " ".join(args), flush=True)
    try:
        subprocess.run([exe, *args], check=True, env=env)
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nQuarto stopped with exit status {e.returncode}.\n"
                 "If the messages above mention 'out of memory' or 'heap', try again with:\n"
                 "    py tools\\render_all.py --web-only --memory-gb 16\n"
                 "or, to build the site without the search index:\n"
                 "    py tools\\render_all.py --web-only --no-search")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web-only", action="store_true")
    ap.add_argument("--docx-only", action="store_true")
    ap.add_argument("--memory-gb", type=int, default=0, help="raise Quarto's memory limit, e.g. 16")
    ap.add_argument("--no-search", action="store_true", help="build the HTML site without the search index")
    a = ap.parse_args()
    global MEMORY_GB
    MEMORY_GB = a.memory_gb

    profiles = sorted(p.name[len("_quarto-"):-len(".yml")] for p in ROOT.glob("_quarto-docx-*.yml"))
    if not a.web_only:
        if not (ROOT / "reference1.docx").exists():
            sys.exit("reference1.docx is missing from the project folder (it is the Word style template named in _quarto.yml).")
        if not profiles:
            sys.exit("No _quarto-docx-*.yml files found. Run: python tools/build_book.py --src source --out .")

    if not a.docx_only:
        quarto("render", "--profile", "web,nosearch" if a.no_search else "web", "--to", "html")   # clears and rebuilds _book/

    if not a.web_only:
        for prof in profiles:
            quarto("render", "--profile", prof, "--to", "docx")
            found = list((ROOT / "_docx" / prof[len("docx-"):]).glob("*.docx"))
            if not found:
                sys.exit(f"No .docx was produced for profile {prof}")
            for f in found:
                print(f"   built {f.relative_to(ROOT)}   (kept private; open in Word and save as PDF into the pdf/ folder)")

    # ---- public downloads: PDFs only
    dest = ROOT / "_book" / "downloads"
    pdf_dir = ROOT / "pdf"
    pdf_dir.mkdir(exist_ok=True)
    if (ROOT / "_book").exists():
        dest.mkdir(parents=True, exist_ok=True)
        for stray in dest.glob("*.docx"):
            stray.unlink()
            print(f"   removed {stray.name} from _book/downloads (Word files are not published)")
        for f in sorted(pdf_dir.glob("*.pdf")):
            shutil.copy2(f, dest / f.name)
            print(f"   copied {f.name} -> _book/downloads/")

    print("\nDone. Commit and push the project (including _book/) to GitHub; Netlify will publish it.")


if __name__ == "__main__":
    main()
