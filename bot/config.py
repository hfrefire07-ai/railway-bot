from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(parsed, minimum)


@dataclass(frozen=True)
class Settings:
    token: str
    prefix: str = "."
    max_input_bytes: int = 1024 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024
    # Discord's own hard cap for a normal (non-boosted) server is 10 MiB per
    # attachment. The bot always zips the result before sending it (see
    # discord_bot._send_result), and refuses to upload if the zip itself is
    # still bigger than this — raise it via DISCORD_UPLOAD_BYTES if your
    # server has boosts that allow bigger attachments.
    discord_upload_bytes: int = 10 * 1024 * 1024
    # Long defaults let large protected scripts finish their full trace instead
    # of silently falling back to the short budget used for interactive samples.
    process_timeout_seconds: int = 7200
    trace_budget_seconds: int = 3600
    # Keep a single expensive devirtualization job from competing for Railway
    # memory with another job; operators can raise this via MAX_CONCURRENT_JOBS.
    max_concurrent_jobs: int = 1
    raw_download_timeout_seconds: int = 900
    max_redirects: int = 3

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            token=os.getenv("DISCORD_BOT_TOKEN", ""),
            prefix=os.getenv("DISCORD_PREFIX", "."),
            max_input_bytes=_int_env("MAX_INPUT_BYTES", 1024 * 1024 * 1024),
            max_output_bytes=_int_env("MAX_OUTPUT_BYTES", 1024 * 1024 * 1024),
            discord_upload_bytes=_int_env("DISCORD_UPLOAD_BYTES", 10 * 1024 * 1024),
            process_timeout_seconds=_int_env("DEOB_TIMEOUT_SECONDS", 7200),
            trace_budget_seconds=_int_env("DEOB_BUDGET_SECONDS", 3600),
            max_concurrent_jobs=_int_env("MAX_CONCURRENT_JOBS", 1),
            raw_download_timeout_seconds=_int_env("RAW_DOWNLOAD_TIMEOUT_SECONDS", 900),
            max_redirects=_int_env("MAX_REDIRECTS", 3),
        )