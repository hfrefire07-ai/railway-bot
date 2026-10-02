# Luau Deobfuscator Discord Bot

A Discord bot for analyzing Luau scripts that you own or are authorized to
inspect. It includes the Luraph v15, IronBrew1, and generic trace pipelines.
The runtime always extracts and uses `vendor/Deobfuscator.zip`; that archive
is part of this source package.

## Commands

- `.lph <raw-url>` analyzes a public raw URL.
- `.lph` with a `.lph`, `.txt`, `.lua`, or `.luau` attachment analyzes that file.
- `.lph --fast <raw-url>` skips the expensive devirtualization path.
- Full devirtualization is the default, including for large inputs.
- `.lph --full <raw-url>` explicitly requests the full path.
- `.lph --obfuscator luraph_v15|ironbrew1|generic <raw-url>` forces a plugin.
- `.help` shows the command reference.

Every successful result is returned as a uniquely named `.txt` attachment and
mentions the user who started the job. Generated output begins with a small
`Deobf by Luau Deobfuscator` header; it does not add decorative separators or
AI branding.

The engine runs the input in its controlled Luau analysis environment and
never executes the generated output as a second payload. Pastefy uploads,
`loadstring` generation, and automatic payload execution are intentionally not
included: they would send or run arbitrary code outside the analysis boundary.

## Limits

There is no safe or technically honest way to promise unlimited processing:
Discord, the host, memory, and the Luau runtime all have platform limits.
Inputs from public raw URLs are streamed to disk, and private or loopback
network destinations are rejected. The defaults are intentionally generous and
can be adjusted with environment variables.

```text
DISCORD_BOT_TOKEN=...
DISCORD_PREFIX=.
MAX_INPUT_BYTES=1073741824
MAX_OUTPUT_BYTES=1073741824
DISCORD_UPLOAD_BYTES=10485760
DEOB_TIMEOUT_SECONDS=7200
DEOB_BUDGET_SECONDS=3600
MAX_CONCURRENT_JOBS=2
RAW_DOWNLOAD_TIMEOUT_SECONDS=900
MAX_REDIRECTS=3
```

The defaults allow up to 2 hours for an engine run and 1 hour for the traced
script. Increase `DEOB_TIMEOUT_SECONDS` or `DEOB_BUDGET_SECONDS` for longer
jobs. Discord, the host, memory, the runtime's loop/stall guards, and Discord's
attachment cap still apply. For larger input files, use a public raw URL
instead of a Discord attachment.

## Run locally

1. Provide `DISCORD_BOT_TOKEN` through your local environment or secret manager; never commit it.
2. Enable **Message Content Intent** in the Discord developer portal.
3. Run `uv sync --locked --no-dev`, then `uv run --locked python main.py`.
## Deploy to Railway

The repository-root `Dockerfile` builds this bot as a Railway worker. Keep the
Railway service root directory at the repository root so Railway detects it.
The image compiles the bundled Luau runtime during the build; a missing source
archive, compiler error, or stale Python lockfile therefore fails the build
instead of appearing later as a running-but-broken bot. Compiler parallelism is
limited to two jobs to reduce builder memory use.

1. Create a Railway service from this repository and leave its root directory
   at `/`.
2. Add `DISCORD_BOT_TOKEN` in that service's **Variables**. Do not commit the
   token or put it in the Dockerfile.
3. Deploy and check the logs for `Connected as ...`. In the Discord developer
   portal, **Message Content Intent** must be enabled for prefix commands.
4. After Railway connects successfully, stop the `Luau Discord Bot` workflow
   in Replit so two copies do not process the same Discord commands.
5. Keep the Railway service to one replica. This is a background worker: it
   does not serve HTTP, so it does not need a public domain, `PORT`, or an HTTP
   health check.

The Docker build and Luau compile cannot be run by Railway until it receives the
repository. The root Dockerfile performs both as required build steps so Railway
will stop before deploying if either one fails.