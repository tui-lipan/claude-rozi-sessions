#!/usr/bin/env python3
"""Install rozi's Claude Code plugin, whose hooks report the live state of the conversation a
Claude client shows, when the user asks for it from the command palette.

Everything goes through Claude Code's public `claude plugin` commands. Both steps are idempotent,
so running the command again is harmless.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_sessions as cs  # noqa: E402

# Cloning the marketplace repository can take a while on a slow connection.
INSTALL_TIMEOUT = 180.0


def install(cli: cs.Cli) -> None:
    """Add rozi's marketplace and install its plugin, then say what happened."""
    claude = cli.settings.claude
    try:
        if cli.hooks_installed():
            cli.offer_command(cs.INSTALL_HOOKS_COMMAND, False)
            cli.notify("rozi's Claude Code hooks are already installed.")
            return
        cli.run(
            [claude, "plugin", "marketplace", "add", cs.HOOKS_MARKETPLACE], timeout=INSTALL_TIMEOUT
        )
        cli.run(
            [claude, "plugin", "install", f"{cs.HOOKS_PLUGIN}@{cs.HOOKS_PLUGIN}"],
            timeout=INSTALL_TIMEOUT,
        )
    except cs.SessionsError as error:
        cli.notify(f"Could not install rozi's Claude Code hooks: {error}", error=True)
        return
    # Nothing left to offer: the command leaves the palette until the plugin is missing again.
    cli.offer_command(cs.INSTALL_HOOKS_COMMAND, False)
    cli.notify(
        "Installed rozi's Claude Code hooks. Restart Claude Code in its panes to start them."
    )


def main() -> int:
    if os.environ.get("ROZI_EXTENSION") != cs.EXTENSION_ID:
        print(f"{cs.EXTENSION_ID} must be launched by Rozi", file=sys.stderr)
        return 2
    install(cs.Cli(cs.Settings.from_environment(dict(os.environ))))
    # Every outcome was reported with `rozi notify`; a failing exit would add a vaguer second one.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
