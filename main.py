from bot.bootstrap import ensure_luau_runtime
from bot.discord_bot import run


if __name__ == "__main__":
    ensure_luau_runtime()
    run()
