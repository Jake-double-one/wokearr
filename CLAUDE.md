# Working on Wokearr

## No real titles, anywhere

Nothing that goes to GitHub may name a movie or series from a real library -
in particular not from the maintainer's own Plex library, whose titles show
up in logs and screenshots shared while debugging.

This covers everything: `CHANGELOG.md`, the READMEs, commit messages, pull
request titles and descriptions, comments, release notes, tests, fixtures,
code comments and screenshots.

- **Changelog entries stay neutral.** Describe the behaviour, not the case
  that revealed it: "a title without an agent poster (typically a season)",
  never the title itself.
- **Examples and test data use placeholders:** `Example Movie`,
  `Example Show`, `Season 1`. Scores, keys and URLs are made up to match.
- **Screenshots** show invented titles (see `docs/screenshot.png`), never a
  real library.
- A title seen in a pasted log or screenshot is for diagnosis only. It does
  not get quoted back into anything that is committed or posted.
