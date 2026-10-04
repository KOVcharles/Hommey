# Local Markdown dependencies

Pinned browser distributions; no runtime CDN or npm installation is required.

- [Marked](https://marked.js.org/), 18.0.14, MIT. File: `marked-18.0.14.js`.
- [DOMPurify](https://github.com/cure53/DOMPurify), 3.4.16, Apache-2.0 OR MPL-2.0. File: `dompurify-3.4.16.js`.

The original license texts are included alongside the bundles. `manifest.json`
records registry URLs, verified package SHA-512 integrity and bundle SHA-256.
To update, retrieve a pinned official npm tarball, verify its registry integrity,
copy the browser distribution and license, update the manifest and template paths,
then run `tests/check_output_experience_ui.cjs` (including the injection cases).

Marked parses Markdown; it does not sanitize HTML. All rendering must go through
`HommeyMarkdown.render`, which uses DOMPurify and a restricted tag/attribute list.
