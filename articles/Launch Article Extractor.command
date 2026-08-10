#!/bin/zsh

# Finder entry point. Automator should call run_article_extractor.zsh directly;
# both routes use the same non-interactive dependency bootstrap.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/zsh "$SCRIPT_DIR/run_article_extractor.zsh"
