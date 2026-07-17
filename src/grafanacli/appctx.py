"""The per-invocation DI container.

Named `appctx`, not `context`, deliberately: `commands/context.py` is the
user-facing sticky-defaults feature. OpenProject has three colliding meanings of
"context" (typer.Context, AppContext, the sticky context) with two of the modules
one directory apart, forcing `from .commands import context as context_cmd` at
the import site. Cheap to get right at scaffold time, annoying later.
"""

from __future__ import annotations

import os
import sys

from agentcli import Emitter, OutputFormat
from agentcli.errors import ConfigError

from . import __version__
from .client import Client
from .config import Config
from .spec import SPEC, credentials, token_url


class AppContext:
    """Lazily-built config, credentials and client for one command run."""

    def __init__(
        self,
        *,
        output: OutputFormat | None = None,
        color: bool = True,
        interactive: bool = False,
    ) -> None:
        self.config = Config.load()
        self.color = color
        self.interactive = interactive
        self.output = self._resolve_format(output)
        self.emitter = Emitter(
            self.output,
            color=color,
            fields=self._resolve_fields(),
            stream=os.environ.get("GRAFANACLI_STREAM") == "1",
        )
        self._client: Client | None = None
        if self.interactive:
            # Same gate as the format prompt, and after it on purpose: one first
            # run asks at most two questions, in the order they matter.
            self._maybe_offer_claude_skill()

    # ---- format ------------------------------------------------------

    def _resolve_format(self, explicit: OutputFormat | None) -> OutputFormat:
        """Precedence: --format/-o > $GRAFANACLI_CLI_FORMAT > $GRAFANACLI_FORMAT
        > saved default > ask once > json.

        NOTE the explicit rungs do NOT swallow their own errors. OpenProject
        wrapped the whole chain in `except ValueError: pass`, so `-o /tmp/x.pdf`
        silently degraded to json instead of complaining — which is how a
        wrong-file write shipped for four releases. A bad --format is a usage
        error; say so. A bad *stored* value is different: it must not brick every
        command, so those rungs do fall through.
        """
        if explicit is not None:
            return explicit
        cli_fmt = os.environ.get("GRAFANACLI_CLI_FORMAT")
        if cli_fmt:
            return OutputFormat.coerce(cli_fmt)  # raises -> caught by main(), exit 1
        env = SPEC.getenv("FORMAT")
        if env:
            try:
                return OutputFormat.coerce(env)
            except ValueError:
                pass  # a stale env var must not brick every command
        saved = self.config.default_format
        if saved:
            try:
                return OutputFormat.coerce(saved)
            except ValueError:
                pass
        if self.interactive:
            chosen = self._ask_default_format()
            if chosen:
                return chosen
        return OutputFormat.json

    def _ask_default_format(self) -> OutputFormat | None:
        """First run, once, on a real TTY. Prompt on stderr; stdout stays clean."""
        try:
            sys.stderr.write(
                "\nFirst run — how should output be formatted by default?\n"
                "  json      machine-readable (default; best for agents & scripts)\n"
                "  table     human-readable\n"
                "  markdown  for pasting into docs/PRs\n"
                "Choice [json]: "
            )
            sys.stderr.flush()
            ans = (sys.stdin.readline() or "").strip().lower()
        except Exception:
            return None
        fmt = OutputFormat.json
        if ans:
            try:
                fmt = OutputFormat.coerce(ans)
            except ValueError:
                fmt = OutputFormat.json
        try:
            self.config.default_format = fmt.value
            self.config.save()
            sys.stderr.write("Saved. Change it any time: `graf settings set-format <fmt>`\n\n")
        except Exception:
            pass  # a first-run nicety must never fail a real command
        return fmt

    # ---- first-run Claude offer --------------------------------------

    def _maybe_offer_claude_skill(self) -> None:
        """Offer to register the Claude Code skill. Once, ever, on a real TTY.

        Without this, `graf install claude` is discoverable only by someone who
        already read the help far enough to know it exists — i.e. the person who
        least needs telling. The skill is what makes Claude reach for this CLI at
        all, so a user who never learns it exists gets none of the tool.

        The ordering below is the whole design, and it is deliberately paranoid:

        * `claude_prompted` is set and SAVED *before* the prompt, so a decline, a
          Ctrl-C, an install that explodes and a config write that fails all count
          as "asked". Re-asking is the failure mode that matters — a nagging CLI
          gets `2>/dev/null`'d and then its errors are invisible too, which is a
          far worse outcome than a skill nobody installed.
        * The prompt goes to **stderr**: stdout is the machine channel, and a
          question printed into it is a parse error for whatever is reading.
        * Everything is swallowed. A nicety must never be the reason a real
          command fails, and this runs before every command.
        """
        try:
            # Local import: commands.install -> commands._shared -> appctx, so a
            # module-level import here is a cycle.
            from .commands import install

            if self.config.claude_prompted:
                return
            if not install.claude_available() or install.skill_installed():
                return

            self.config.claude_prompted = True
            self.config.save()  # if this fails we ask again -- but we install nothing

            sys.stderr.write(
                "\nClaude Code is installed here. Register `graf` as a skill, so Claude\n"
                "uses it automatically when you mention Grafana, Loki logs or dashboards?\n"
                "  writes ~/.claude/skills/grafana/SKILL.md — undo with "
                "`graf install claude --uninstall`\n"
                "Install it? [y/N]: "
            )
            sys.stderr.flush()
            ans = (sys.stdin.readline() or "").strip().lower()
            if ans not in ("y", "yes"):
                # Not a dead end: the answer to "how do I get this later" has to
                # be on screen, or declining once means never finding it again.
                sys.stderr.write(
                    "Skipped — you will not be asked again. "
                    "Change your mind any time: `graf install claude`\n\n"
                )
                return
            path = install.write_skill()
            sys.stderr.write(f"Installed {path}\nStart a new Claude session to pick it up.\n\n")
        except Exception:
            pass  # a first-run nicety must never fail a real command

    @staticmethod
    def _resolve_fields() -> list[str] | None:
        raw = os.environ.get("GRAFANACLI_CLI_FIELDS")
        if not raw:
            return None
        return [f.strip() for f in raw.split(",") if f.strip()]

    # ---- client ------------------------------------------------------

    def client(self) -> Client:
        if self._client is None:
            prof = self.config.resolve()
            token = credentials.get_token(self.config.active_profile_name())
            if not token:
                raise ConfigError(
                    "no API token. Run `graf auth login`, or set GRAFANA_TOKEN "
                    f"(create a service account at {token_url(prof.base_url)})."
                )
            self._client = Client(
                prof.base_url,
                token,
                org_id=prof.org_id,
                verify_ssl=prof.verify_ssl,
                dry_run=os.environ.get("GRAFANACLI_DRY_RUN") == "1",
                user_agent=f"agent-tool-grafana-cli/{__version__}",
            )
        return self._client
