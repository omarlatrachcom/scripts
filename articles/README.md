# Article & Comments Extractor

This application starts with a website-selection screen. The user explicitly
selects a configured website and clicks **Open selected website extractor**.
Only then does that website's dedicated URL/output screen open. The application
never guesses an extractor from a pasted URL.

Current website menu:

- **Ana Toledo — Mira!:** full article plus all publicly accessible comments and
  replies from `anatoledo.substack.com`.
- **The Reese Report:** complete public articles plus all publicly accessible
  comments and nested replies from `gregreese.substack.com`. Video posts and
  other non-linear content retain their reading position as ordered
  `IMAGE-XX` placeholders.
- **TomatoBible(トマトバイブル):** complete public articles from
  `tomatobible.substack.com`, with images, video embeds, interactive buttons,
  tables, and other non-linear content represented by ordered placeholders. It
  does not extract comments.
- **OffGuardian:** complete public articles plus every publicly accessible
  comment and nested reply from `off-guardian.org`. The title image and all
  article-body media stay in reading order as `IMAGE-XX` placeholders, and each
  reply records its direct parent comment ID.
- **Globalresearch:** complete public articles from `globalresearch.ca`, with
  the featured image, body images, galleries, tables, embeds, audio, video, and
  other non-linear elements retained in source order as `IMAGE-XX`
  placeholders. It does not extract comments.
- **Telquel.ma:** complete public dated articles from `telquel.ma`, preserving
  the standfirst, paragraphs, headings, lists, and quotations. Lazy-loaded
  images, tables, embeds, in-body ad/newsletter components, and related-story
  cards stay in source order as `IMAGE-XX` placeholders. It does not extract
  comments.
- **Food Sovereignty | Agrarian Systems | Development:** Colin Todhunter's
  `off-guardian.org` archive, exposed as its own explicit extractor choice.
  Complete public articles, ordered non-linear placeholders, and all public
  comments and nested replies are exported without URL-based adapter selection.

## Deleting a website extractor

Select a website on the first screen and use the red **Delete website** button
beside **Open selected website extractor**. After an explicit warning and
confirmation, the application atomically edits `article_extractor_gui.py` and
physically removes that website's profile and dedicated adapter code. Code still
used by another installed website is retained and is removed only after its
final dependent website is deleted.

Existing extraction folders and their media are never touched. Website deletion
cannot be undone inside the GUI. Restore the script from GitHub or reinstall it
to bring a deleted extractor back. The operation requires the Python source file
to be writable and validates that the remaining program still compiles before
replacing it.

Each future source with a distinct article or comment system gets its own small
adapter. Adding one does not require another GUI or changes to the output and
chunking engine.

## Website prompt generator

The main website-selection screen includes **Generate prompt to add a website**.
It opens a second screen that collects:

- An optional editable website name. If left empty, it is inferred automatically
  from the supplied page metadata or domain.
- An optional homepage/archive URL.
- One required example article URL.
- An explicit comments choice: no comments, comments only, or comments with
  nested replies.

The generated implementation prompt appears in an editable preview and can be
copied to the clipboard for pasting into Codex. Its instructions preserve this
application's website selector, media-download option, combined-comments format,
safe token chunking, and exact `IMAGE-XX` layout. Form values are remembered in
`~/Library/Application Support/ArticleExtractor/website_prompt_draft.json`.

## Run it

On macOS, double-click `Launch Article Extractor.command`. The launcher does not
depend on Terminal profile files and performs these steps automatically:

1. Finds an installed Python that has Tkinter support.
2. If necessary and Homebrew is available, installs `python` and `python-tk`.
3. Creates a private environment under
   `~/Library/Application Support/ArticleExtractor`.
4. Installs the exact token counter and TelQuel's browser-compatible HTTP
   transport in that private environment.
5. Opens the GUI.

It writes startup and dependency details to
`~/Library/Logs/scripts-apps/article-extractor.log`. Bootstrap failures appear in
a native macOS dialog, so a Terminal window is not required.

You can also run the Python file directly when Python and Tkinter are already
available:

```bash
python3 article_extractor_gui.py
```

Select the required website, open its dedicated extractor, and paste an
individual article URL. For example, Ana Toledo accepts:

```text
https://anatoledo.substack.com/p/freemasons-in-france-convicted
```

Choose an existing output folder and click **Extract article and comments**.
The app creates a new subfolder named from the article slug. Repeated extractions
use `-2`, `-3`, and so on, so an earlier extraction is never overwritten.
The initial output folder is `~/Downloads`.

For **The Reese Report**, use an individual URL under `/p/`. The dedicated
adapter reads the complete public article body from Substack's post endpoint,
represents a post's primary video at the start as `IMAGE-01`, and then preserves
the remaining text and non-linear content in source order. It also reads the
public comments endpoint and exports every accessible comment and nested reply,
including the direct parent comment ID for each reply.

For **TomatoBible(トマトバイブル)**, use an individual URL under `/p/`. The
dedicated article-only adapter reads the complete public `body_html` from
Substack's `/api/v1/posts/{slug}` endpoint. It preserves the editorial text and
code blocks, and replaces figures, YouTube players, tables, interactive buttons,
and similar non-linear elements in place with `IMAGE-XX` placeholders. It never
requests or exports comments.

For **OffGuardian**, use an individual dated article URL under
`/YYYY/MM/DD/article-slug/`. The dedicated adapter discovers the post's public
WordPress REST endpoint from the live page, reads the complete rendered article
body, and uses the page's real byline and title image. It then paginates the
public approved-comments endpoint until every accessible comment and reply is
exported. The comments file records `Reply to comment ID` for every nested reply.

For **Globalresearch**, use an individual article URL in the form
`https://www.globalresearch.ca/article-slug/numeric-post-id`. The dedicated
article-only adapter reads the complete rendered body, author, and featured
image from the site's public WordPress `/wp-json/wp/v2/posts/{id}?_embed=1`
endpoint. It tries that first-party endpoint directly; if Globalresearch's
Cloudflare policy blocks the request, it retries the same public resource
through Jina Reader. The adapter recognizes the indented paragraphs used by the
site for quotations, preserves linear structure, and never requests or exports
comments. When media download is selected, original media URLs are tried first;
region-blocked Globalresearch files fall back to their latest raw Wayback
capture without changing the source URL printed in the article text.

For **Telquel.ma**, use an individual dated article URL in the form
`https://telquel.ma/YYYY/MM/DD/article-slug_numeric-id`. TelQuel advertises a
WordPress REST API, but its public REST and oEmbed routes currently answer with
`401 Unauthorized`, while its category RSS exposes only article excerpts. The
dedicated article-only adapter therefore reads the complete server-rendered
body from the live page's first `.single-content > .col-large` article column.
It excludes navigation, sharing controls, the repeated author footer, tags, and
recommendation rails; non-linear components inside the article column retain
their exact position as placeholders. The launcher installs `curl-cffi`, whose
browser-compatible transport is required by TelQuel's Cloudflare edge. No
comment endpoint is requested.

For **Food Sovereignty | Agrarian Systems | Development**, explicitly select
that entry on the first screen, then use a dated Colin Todhunter article URL from
the archive at `https://off-guardian.org/category/colin-todhunter/`. Its adapter
uses the post endpoint advertised by the live page and the public WordPress
approved-comments collection. It preserves each API `parent` value as the
direct parent comment ID; it does not choose itself from the pasted URL.

Enable **Download media files when a direct file is available** if image, audio,
or video files should also be saved. It is off by default. Downloaded files go in
the extraction's `media/` folder.

## Output

- One article `.txt` file, or numbered `_part_XX.txt` files when chunking is
  required.
- One structured `_comments.txt` file containing every public comment and reply.
  This is created only by comment-enabled extractors. If the combined document
  exceeds the token ceiling, it becomes numbered `_comments_part_XX.txt` files,
  split between complete comments.
- The source URL appears directly below each `IMAGE-XX` placeholder, separated
  from the placeholder by an empty line. There is no separate media manifest.
  OffGuardian and Food Sovereignty | Agrarian Systems | Development render the
  source as a Markdown link in the exact form `Media source: [URL](URL)`.
  Globalresearch and Telquel.ma use that exact form as well.
- An optional `media/` folder containing downloaded direct media files.
- `extraction_summary.json` with counts and the token-counting mode.

Images, tables, diagrams, videos, embeds, and similar non-linear elements become
ordered `IMAGE-XX` placeholders at their original position. The source URL is
written beneath the placeholder after an empty line. Downloaded files use the
placeholder as their filename inside `media/`, so extra type, location, and
local-filename lines are unnecessary. Headings, paragraphs, lists, and
quotations remain separate blocks.

Every text file is kept strictly below 12,500 tokens. Chunk boundaries occur only
between paragraph-like blocks, and headings stay with the following block. If a
single source paragraph itself exceeds the limit, extraction stops with a clear
error rather than cutting through the paragraph.

The launcher installs `tiktoken` and `curl-cffi` in an isolated environment
under `~/Library/Application Support/ArticleExtractor`. With `tiktoken`, the app
counts using `cl100k_base`; otherwise it uses UTF-8 byte length as a conservative
upper bound. The fallback can produce smaller chunks but will not undercount a
byte-based GPT tokenizer. `curl-cffi` is only required by the TelQuel adapter.

Manual dependency installation:

```bash
python3 -m pip install -r requirements.txt
```

## Automator

Create an Automator **Application**, add **Run Shell Script**, choose `/bin/zsh`,
and use this command:

```zsh
/bin/zsh "/Users/omar/Documents/scripts/articles/run_article_extractor.zsh"
```

Automator should call `run_article_extractor.zsh`, not the `.command` wrapper.
The runner has its own fixed `PATH`, needs no interactive terminal, installs
missing dependencies through Homebrew, and shows native dialogs for failures.

Installing Homebrew itself cannot be safely automated from Automator because its
first installation may require administrator approval. On this Mac Homebrew is
already installed, so Python/Tk dependency setup is automatic.

For test or automation use, the same script has a command-line mode:

```bash
python3 article_extractor_gui.py \
  --website "ana-toledo" \
  --download-media \
  --url "https://anatoledo.substack.com/p/freemasons-in-france-convicted" \
  --output "$HOME/Documents"
```

The Reese Report article-and-comments example:

```bash
python3 article_extractor_gui.py \
  --website "reese-report" \
  --url "https://gregreese.substack.com/p/cymatics-and-the-mysteries-of-the" \
  --output "$HOME/Downloads"
```

TomatoBible article-only example:

```bash
python3 article_extractor_gui.py \
  --website "tomatobible" \
  --url "https://tomatobible.substack.com/p/predators-among-us-fascists-decodedunderstanding-b3d" \
  --output "$HOME/Downloads"
```

OffGuardian article-and-comments example:

```bash
python3 article_extractor_gui.py \
  --website "offguardian" \
  --url "https://off-guardian.org/2026/07/25/i-no-longer-trust-anyone/" \
  --output "$HOME/Downloads"
```

Globalresearch article-only example:

```bash
python3 article_extractor_gui.py \
  --website "globalresearch" \
  --url "https://www.globalresearch.ca/living-most-corrupt-democracy-imagined/5934366" \
  --output "$HOME/Downloads"
```

TelQuel article-only example:

```bash
python3 article_extractor_gui.py \
  --website "telquel" \
  --url "https://telquel.ma/2026/07/24/le-reflexe-jettou_2001434" \
  --output "$HOME/Downloads"
```

Food Sovereignty | Agrarian Systems | Development article-and-comments example:

```bash
python3 article_extractor_gui.py \
  --website "food-sovereignty" \
  --url "https://off-guardian.org/2026/07/24/manufacturing-inevitability-breaking-the-myth-of-no-alternative/" \
  --output "$HOME/Downloads"
```

## Adding another site

Create the source's adapter, then add a `WebsiteExtractorDefinition` to
`WEBSITE_EXTRACTORS`. That creates a new entry on the first screen. The shared
HTML conversion, placeholder numbering, safe chunking, GUI shell, and writers
remain reusable.
