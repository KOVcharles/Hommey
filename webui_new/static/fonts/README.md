# Login headline font

`hommey-editorial.ttf` is a self-hosted, regular-weight subset of Noto Serif SC,
obtained from Google Fonts. It contains only the headline characters:

出发前，把一切理清。

Source: https://fonts.google.com/specimen/Noto+Serif+SC
License: SIL Open Font License 1.1, included as `OFL-NotoSerifSC.txt`.

The font is restricted to the editorial headline in `auth.css`; form controls
use system sans-serif fonts. If the headline changes, regenerate the subset
for the new text. No external font request is made by the login page.
