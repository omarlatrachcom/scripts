# Article & Comments Extractor

This application starts with a website-selection screen. The user explicitly
selects a configured website and clicks **Open selected website extractor**.
Only then does that website's dedicated URL/output screen open. The application
never guesses an extractor from a pasted URL.

Current website menu:

- **Ana Toledo — Mira!:** full article plus all publicly accessible comments and
  replies from `anatoledo.substack.com`.
- **islamonline.net/category/books:** complete public book-category articles from
  `islamonline.net`. This dedicated extractor exports the article only; it does
  not create a comments file.
- **islamonline.net/category/sharia:** complete public Sharia-category articles
  from `islamonline.net`. This is a separate article-only extractor and creates
  no comments file.
- **إسلام أون لاين:** complete public articles from the separate
  `fiqh.islamonline.net` publication. It exports article text only and does not
  treat the publication's generic social-sharing logo as article media.
- **InfoQ:** complete public podcast articles from `infoq.com`, including the
  introduction, key takeaways, direct podcast audio, and full transcript. It
  does not extract comments.

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
4. Installs the exact token-counting dependency in that private environment.
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

For **islamonline.net/category/books** or **islamonline.net/category/sharia**,
use an individual article from the selected category. Their buttons read
**Extract article**, and their exports contain no comments file.

For **إسلام أون لاين**, use an individual article hosted specifically on
`fiqh.islamonline.net`. This explicit choice does not accept main-site
`islamonline.net` URLs and does not create comments files.

For **InfoQ**, use an individual URL under `/podcasts/`. The extractor omits
page controls and recommendations, preserves the editorial transcript, and
uses InfoQ's public podcast feed to resolve a downloadable MP3 when available.

Enable **Download media files when a direct file is available** if image, audio,
or video files should also be saved. It is off by default. Downloaded files go in
the extraction's `media/` folder.

## Output

- One article `.txt` file, or numbered `_part_XX.txt` files when chunking is
  required.
- One structured `_comments.txt` file containing every public comment and reply.
  If the combined document exceeds the token ceiling, it becomes numbered
  `_comments_part_XX.txt` files, split between complete comments.
- The source URL appears directly below each `IMAGE-XX` placeholder, separated
  from the placeholder by an empty line. There is no separate media manifest.
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

The launcher installs the optional `tiktoken` counter in an isolated environment
under `~/Library/Application Support/ArticleExtractor`. With `tiktoken`, the app
counts using `cl100k_base`; otherwise it uses UTF-8 byte length as a conservative
upper bound. The fallback can produce smaller chunks but will not undercount a
byte-based GPT tokenizer.

Optional exact token counter:

```bash
python3 -m pip install tiktoken
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

IslamOnline Books article-only example:

```bash
python3 article_extractor_gui.py \
  --website "islamonline-books" \
  --url "https://islamonline.net/%d8%b9%d8%b1%d8%b6-%d9%83%d8%aa%d8%a7%d8%a8-%d8%aa%d9%88%d8%b8%d9%8a%d9%81-%d8%a7%d9%84%d8%b0%d9%83%d8%a7%d8%a1-%d8%a7%d9%84%d8%aa%d9%88%d9%84%d9%8a%d8%af%d9%8a-%d9%81%d9%8a-%d8%a7%d9%84%d8%a7%d8%ac/" \
  --output "$HOME/Downloads"
```

IslamOnline Sharia article-only example:

```bash
python3 article_extractor_gui.py \
  --website "islamonline-sharia" \
  --url "https://islamonline.net/%d9%81%d8%ac%d8%a3%d8%a9-%d9%86%d9%82%d9%85%d8%a9-%d8%a7%d9%84%d9%84%d9%87-%d8%b9%d8%b2-%d9%88%d8%ac%d9%84/" \
  --output "$HOME/Downloads"
```

IslamOnline Fiqh article-only example:

```bash
python3 article_extractor_gui.py \
  --website "islamonline-fiqh" \
  --url "https://fiqh.islamonline.net/%d8%a5%d8%b9%d8%b7%d8%a7%d8%a1-%d8%a7%d9%84%d9%81%d9%82%d9%8a%d8%b1-%d9%85%d9%86-%d8%a7%d9%84%d8%b2%d9%83%d8%a7%d8%a9-%d9%84%d9%84%d8%b2%d9%88%d8%a7%d8%ac/" \
  --output "$HOME/Downloads"
```

InfoQ podcast article-only example:

```bash
python3 article_extractor_gui.py \
  --website "infoq-podcasts" \
  --url "https://www.infoq.com/podcasts/strands-agents/" \
  --output "$HOME/Downloads"
```

## Adding another site

Create the source's adapter, then add a `WebsiteExtractorDefinition` to
`WEBSITE_EXTRACTORS`. That creates a new entry on the first screen. The shared
HTML conversion, placeholder numbering, safe chunking, GUI shell, and writers
remain reusable.
