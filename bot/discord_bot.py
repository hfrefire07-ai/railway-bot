from __future__ import annotations

import asyncio
import logging
import secrets
import shlex
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord.ext import commands

from .config import Settings
from .downloader import DownloadError, download_to_file
from .runner import DeobfuscationError, DeobfuscationResult, run_deobfuscator

ALLOWED_EXTENSIONS = {".lua", ".luau", ".txt", ".lph"}
SUPPORTED_OBFUSCATORS = {"luraph_v15", "ironbrew1", "generic"}

# Plain-text output with a short random suffix prevents collisions when
# several users upload files with the same name.
OUTPUT_EXTENSION = ".txt"

PURPLE = discord.Color.from_rgb(124, 58, 237)
COLOR_PROCESSING = PURPLE
COLOR_SUCCESS = PURPLE
COLOR_ERROR = PURPLE

MENTION_ALLOWED = discord.AllowedMentions(users=True, everyone=False, roles=False)
LOGGER = logging.getLogger("luau-discord-bot")


def _human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _output_name(original_name: str) -> str:
    """Return a safe, unique plain-text result name."""
    stem = Path(original_name).stem.strip() or "script"
    safe_stem = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in stem
    ).strip("._-") or "script"
    return f"{safe_stem}.deobf-{secrets.token_hex(4)}{OUTPUT_EXTENSION}"


def _status_embed(
    title: str,
    *,
    color: discord.Color,
    description: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    footer: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in fields or ():
        embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    return embed


class LuauBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=settings.prefix,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)

    async def setup_hook(self) -> None:
        await self.add_cog(LuauCommands(self))

    async def on_ready(self) -> None:
        if self.user:
            LOGGER.info("Connected as %s in %d server(s).", self.user, len(self.guilds))


class LuauCommands(commands.Cog):
    def __init__(self, bot: LuauBot):
        self.bot = bot

    @commands.command(name="lph")
    async def lph(self, ctx: commands.Context, *, arguments: str = "") -> None:
        """Deobfuscate a raw URL or the first attached script."""
        status: discord.Message | None = None
        try:
            options, target = self._parse_arguments(arguments)
            attachment = ctx.message.attachments[0] if ctx.message.attachments else None
            if not target and not attachment:
                await ctx.send(
                    embed=_status_embed(
                        "Script required",
                        color=COLOR_ERROR,
                        description=(
                            f"Usage: `{self.bot.settings.prefix}lph <raw-url>` or attach a "
                            "`.lph`, `.txt`, `.lua`, or `.luau` file."
                        ),
                    )
                )
                return
            if target and attachment:
                await ctx.send(
                    embed=_status_embed(
                        "Ambiguous input",
                        color=COLOR_ERROR,
                        description="Use a URL or an attachment, not both.",
                    )
                )
                return

            status = await ctx.send(
                embed=_status_embed(
                    "📥 Received",
                    color=COLOR_PROCESSING,
                    description="Downloading and preparing the analysis…",
                )
            )
            async with self.bot.job_slots:
                await self._process(ctx, status, target, attachment, options)
        except DownloadError as exc:
            await self._fail(ctx, status, "Download failed", str(exc))
        except DeobfuscationError as exc:
            await self._fail(
                ctx,
                status,
                "Luau engine failed",
                str(exc),
                log_path=exc.log_path,
            )
        except ValueError as exc:
            await self._fail(ctx, status, "Invalid options", str(exc))
        except Exception:
            LOGGER.exception("Unexpected command failure")
            await self._fail(
                ctx,
                status,
                "Unexpected error",
                "The job failed unexpectedly. Check the bot log for details.",
            )

    @commands.command(name="help", aliases=["lphhelp"])
    async def help(self, ctx: commands.Context) -> None:
        """Show the short command reference."""
        prefix = self.bot.settings.prefix
        embed = _status_embed(
            "Luau Deobfuscator",
            color=COLOR_PROCESSING,
            description=(
                f"`{prefix}lph <raw-url>` — full analysis; automatically retries a fast trace if Railway kills it.\n"
                f"`{prefix}lph --fast <raw-url>` — skip expensive devirtualization.\n"
                f"`{prefix}lph --full <raw-url>` — require a full VM lift; return diagnostics instead of a trace if it fails.\n"
                f"`{prefix}lph --obfuscator luraph_v15 <raw-url>` — force a plugin.\n"
                f"`{prefix}help` — show this help.\n"
                "You can also attach `.lph`, `.txt`, `.lua`, or `.luau` files.\n"
                "Results are returned as uniquely named `.txt` files."
            ),
            footer=(
                f"Configured input ceiling: "
                f"{_human_size(self.bot.settings.max_input_bytes)}. Discord limits still apply."
            ),
        )
        await ctx.send(embed=embed)

    @staticmethod
    def _parse_arguments(arguments: str) -> tuple[dict[str, str | bool], str]:
        if not arguments.strip():
            return {}, ""
        try:
            tokens = shlex.split(arguments)
        except ValueError as exc:
            raise ValueError("the command quotes could not be parsed.") from exc

        options: dict[str, str | bool] = {}
        target = ""
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--no-devirt":
                options["no_devirt"] = True
            elif token == "--fast":
                options["fast"] = True
            elif token == "--full":
                options["full"] = True
            elif token == "--obfuscator":
                index += 1
                if index >= len(tokens) or tokens[index] not in SUPPORTED_OBFUSCATORS:
                    raise ValueError("obfuscator must be luraph_v15, ironbrew1, or generic.")
                options["obfuscator"] = tokens[index]
            elif token.startswith("--"):
                raise ValueError(f"unknown option: {token}")
            elif target:
                raise ValueError("only one URL is accepted.")
            else:
                target = token
            index += 1
        return options, target

    async def _process(
        self,
        ctx: commands.Context,
        status: discord.Message,
        target: str,
        attachment: discord.Attachment | None,
        options: dict[str, str | bool],
    ) -> None:
        settings = self.bot.settings
        with tempfile.TemporaryDirectory(prefix="lph-input-") as workdir:
            input_path = Path(workdir) / "input"
            if attachment:
                if attachment.size > settings.max_input_bytes:
                    raise DownloadError(
                        f"The attachment exceeds the configured "
                        f"{_human_size(settings.max_input_bytes)}."
                    )
                original_name = attachment.filename
                await download_to_file(
                    attachment.url,
                    input_path,
                    settings,
                    suggested_name=attachment.filename,
                )
            else:
                parsed = urlparse(target)
                original_name = Path(parsed.path).name or "script.luau"
                await download_to_file(target, input_path, settings, suggested_name=original_name)

            input_size = input_path.stat().st_size
            fast = bool(options.get("fast") or options.get("no_devirt"))
            if fast:
                mode = "fast trace (partial)"
            elif options.get("full"):
                mode = "full; automatic fallback disabled"
            else:
                mode = "full, with fast retry if Railway kills it"
            await status.edit(
                embed=_status_embed(
                    f"🔎 Analyzing `{original_name}`",
                    color=COLOR_PROCESSING,
                    fields=[
                        ("Input size", _human_size(input_size), True),
                        ("Mode", mode, True),
                    ],
                    description=(
                        "Running the static analysis VM. If the full pass is killed by the host, "
                        "the bot will retry with a partial fast trace."
                        if not fast and not options.get("full")
                        else "Running the requested static analysis mode…"
                    ),
                )
            )
            result = await run_deobfuscator(
                input_path,
                original_name,
                settings,
                no_devirt=bool(options.get("no_devirt")),
                fast=fast,
                allow_fast_fallback=not bool(options.get("full")),
                forced_obfuscator=options.get("obfuscator") if isinstance(options.get("obfuscator"), str) else None,
            )
            await status.edit(
                embed=_status_embed(
                    "📦 Preparing the result",
                    color=COLOR_PROCESSING,
                    description=(
                        f"Detected: **{result.detected_obfuscator}**. "
                        f"Mode: **{result.mode}**. Preparing the `.txt` result…"
                    ),
                )
            )
            await self._send_result(ctx, status, result, original_name)
            try:
                result.output_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _send_result(
        self,
        ctx: commands.Context,
        status: discord.Message,
        result: DeobfuscationResult,
        original_name: str,
    ) -> None:
        path = result.output_path
        size = path.stat().st_size
        limit = self.bot.settings.discord_upload_bytes
        out_name = _output_name(original_name)
        mention = ctx.author.mention

        if size > limit:
            await status.edit(
                content=None,
                embed=_status_embed(
                    "⚠️ Output is too large",
                    color=COLOR_ERROR,
                    description=(
                        f"`{out_name}` is {_human_size(size)}. Discord's attachment "
                        f"limit here is {_human_size(limit)}; the bot cannot bypass "
                        "that platform limit."
                    ),
                ),
                attachments=[],
            )
            return

        if result.fallback_used:
            description = (
                f"The full pass was terminated by the host (`SIGKILL`); the automatic fast retry "
                f"produced a partial trace in `{out_name}`."
            )
        elif result.partial_reason:
            description = (
                f"The engine could not complete the VM lift, so `{out_name}` contains a partial "
                f"behavior trace. Reason: {result.partial_reason}"
            )
        elif result.mode == "Fast trace (partial)":
            description = f"Fast mode produced a partial behavior trace in `{out_name}`."
        else:
            description = f"`{out_name}` is ready as a plain-text file."

        embed = _status_embed(
            "✅ Done",
            color=COLOR_SUCCESS,
            description=description,
            fields=[
                ("Detected obfuscator", result.detected_obfuscator, True),
                ("Analysis mode", result.mode, True),
                ("Output size", _human_size(size), True),
                ("Elapsed", f"{result.elapsed_seconds:.1f} s", True),
            ],
        )
        await status.edit(
            content=mention,
            embed=embed,
            attachments=[discord.File(str(path), filename=out_name)],
            allowed_mentions=MENTION_ALLOWED,
        )

    async def _fail(
        self,
        ctx: commands.Context,
        status: discord.Message | None,
        title: str,
        description: str,
        *,
        log_path: Path | None = None,
    ) -> None:
        log_file_exists = log_path is not None and log_path.is_file()
        log_size = log_path.stat().st_size if log_file_exists else 0
        if log_file_exists:
            full_log = log_path.read_text(encoding="utf-8", errors="replace")
            LOGGER.error(
                "Complete engine diagnostic log (%s; %s bytes):\n%s",
                log_path.name,
                log_size,
                full_log,
            )
            if log_size > self.bot.settings.discord_upload_bytes:
                description += (
                    "\n\nEl log completo se escribió en los logs del bot, pero no se "
                    f"adjuntó a Discord porque pesa {_human_size(log_size)} y supera "
                    f"el límite configurado de {_human_size(self.bot.settings.discord_upload_bytes)}."
                )
            else:
                description += "\n\nSe adjunta el log completo del motor en formato `.txt`."

        embed = _status_embed(f"❌ {title}", color=COLOR_ERROR, description=description)
        try:
            files = (
                [discord.File(str(log_path), filename="luau-engine-error.txt")]
                if log_file_exists and log_size <= self.bot.settings.discord_upload_bytes
                else []
            )
            if status is not None:
                await status.edit(content=None, embed=embed, attachments=files)
            elif files:
                await ctx.send(embed=embed, files=files)
            else:
                await ctx.send(embed=embed)
        finally:
            if log_file_exists:
                try:
                    log_path.unlink()
                except OSError:
                    LOGGER.warning("Could not remove temporary engine log %s", log_path)

    @lph.error
    async def lph_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            description = "Attach a file or provide a raw URL."
        else:
            description = "The command could not be processed. Use `.help` for syntax."
            LOGGER.error("Command error: %s", error)
        await ctx.send(embed=_status_embed("❌ Error", color=COLOR_ERROR, description=description))


def create_bot(settings: Settings | None = None) -> LuauBot:
    resolved = settings or Settings.from_environment()
    if not resolved.token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing.")
    return LuauBot(resolved)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = create_bot()
    bot.run(bot.settings.token, log_handler=None)
