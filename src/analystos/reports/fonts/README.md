# Bundled report fonts

- `DejaVuSans*.ttf`, `DejaVuSansMono.ttf`: DejaVu Sans (Bitstream Vera / public-domain licence, [LICENSE_DEJAVU](LICENSE_DEJAVU)),
  the primary face: Latin, Latin-extended, Greek, Cyrillic and report symbols, with real bold and oblique styles.
- `NotoSansCJKsc-VF.ttf`: Noto Sans CJK SC from the
  [Noto CJK upstream repository](https://github.com/notofonts/noto-cjk/blob/main/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf)
  under the [SIL Open Font License 1.1](OFL.txt), the fallback for Han, Hiragana, Katakana and Hangul.
  SHA-256 `990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f`.

- `NotoSansTamil-Regular.ttf`: Noto Sans Tamil from the
  [Noto fonts distribution](https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansTamil/hinted/ttf/NotoSansTamil-Regular.ttf)
  under the [SIL Open Font License 1.1](OFL.txt), the second fallback, for Tamil (S08-T6).
  SHA-256 `3c0a186feb3c63c7f6d63e1511dcdc144e745ae09b98e217c83f3e317974f6f9`.

- `NotoSansArabic-Regular.ttf`: Noto Sans Arabic from the
  [Noto fonts distribution](https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansArabic/hinted/ttf/NotoSansArabic-Regular.ttf)
  under the [SIL Open Font License 1.1](OFL.txt), the third fallback: DejaVu Sans has Arabic in its regular face
  only, so bold and oblique Arabic (and anything the coverage check sees across all styles) comes from here.
  SHA-256 `bdff3e5659d67e67def05b33f749683b9376ae819d65d3dd62ac4640b3aaef48`.

Arabic glyphs in regular text come from DejaVu Sans, the rest from Noto Sans Arabic. Joining, right-to-left order and Tamil
vowel-sign reordering need HarfBuzz: with the optional `uharfbuzz` package installed, fpdf2 shapes text;
without it every glyph prints, unshaped.

`fpdf2` embeds only the glyphs each report uses. A character no bundled font covers (other Indic scripts, emoji)
is mapped, folded or printed as `?` by `pdf_text`, never dropped silently.
