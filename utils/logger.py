"""
Central logging setup. Every module grabs its own logger with
get_logger(__name__) instead of reaching for print() directly, so
everything shows up with a consistent timestamp/level/module prefix
and can actually be filtered with journalctl -p, since the bot runs
under systemd and stdout lands there anyway.

Level defaults to INFO. Set MUSICBOT_LOG_LEVEL=DEBUG in the environment
(or .env) to get the noisier stuff too, things like the ffmpeg stderr
passthrough and per-track stream url diagnostics, which is what you
want turned on while chasing something intermittent like the 403s.
"""
import logging
import os
import sys

_CONFIGURED = False


def _configure_root():
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("MUSICBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    # wipe out any handlers a library added before we got here, so we
    # don't end up with the same line printed twice
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # discord.py's own logger is fairly chatty at DEBUG, so leave it at
    # INFO regardless of what we ask for ourselves unless someone
    # explicitly wants full discord.py internals too
    if level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
