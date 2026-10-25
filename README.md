# A Compilation of the Bab's Writings — Quarto book

Live site: https://bab.peopleofbaha.world (GitHub -> Netlify)

## Files you own (the build script never overwrites them)
* `_quarto.yml`         title, footer, themes, DOCX `reference-doc: reference1.docx`, toc depths
* `index.qmd`           the preface
* `assets/custom.scss`  extra HTML styling
* `netlify.toml`, `.gitignore`
* `tools/titles.json`   `{"BB00123": "Title"}` names a writing that has no title in the source
* `tools/headings.json` manual section headings for a writing (replace the automatic ones)
* `tools/options.json`  `{"strip_footnotes": ["BB00151"]}` removes footnotes from listed writings
* `reference1.docx`     Word style template (keep it in this folder)

## Files the script regenerates every run
`chapters/`, `downloads.qmd`, `_quarto-web.yml`, `_quarto-docx-*.yml`, `tools/review_report.md`

## Workflow
1. Put the source volumes in `source/`.
2. `python tools/build_book.py --src source --out .`   then read `tools/review_report.md`
3. `python tools/render_all.py`
   * HTML site -> `_book/`
   * each DOCX edition -> `_docx/...`, and copied into `_book/downloads/` (the Downloads page links to them)
4. Commit and push everything, **including `_book/`**. Netlify only publishes `_book` (no build command), because Netlify does not have Quarto.

## Netlify one-time setup
* Site settings -> Build & deploy: build command empty, publish directory `_book`
* Domain management -> add custom domain `bab.peopleofbaha.world`, then at the DNS host for peopleofbaha.world add a CNAME record `bab` -> `<your-site>.netlify.app`

PDF is not produced by Quarto: open each DOCX in Word and save as PDF.
