#!/bin/bash
# Installs REA (rea-agents) and registers its MCP server with Claude Code
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Global install fills no git-ignored directory, so check the command itself
if ! command -v rea >/dev/null 2>&1; then
  npm install --global rea-agents
fi

# Register REA with Claude Code only when the registration is missing
if ! grep -q '"rea"' "$HOME/.claude.json" 2>/dev/null; then
  (cd /tmp && rea setup --client claude_code --yes >/dev/null)
fi
