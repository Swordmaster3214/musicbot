"""
Wraps discord.FFmpegPCMAudio so we can see what ffmpeg is actually
saying on stderr instead of it disappearing into devnull, which is
what discord.py does with it by default.

This exists because of the occasional 403 on stream urls. Without
this, a track that hits a 403 just goes silent (ffmpeg exits, the
after= callback fires like the song ended normally) with nothing in
our logs pointing at why. With this, the actual ffmpeg error line
(HTTP error 403 Forbidden, connection refused, whatever it turns out
to be) gets logged against the track that hit it.
"""
import subprocess
import threading

from utils.logger import get_logger

logger = get_logger(__name__)


def build_logged_ffmpeg_source(url: str, before_options: str, options: str, track_title: str):
    """
    Same as discord.FFmpegPCMAudio(url, before_options=..., options=...),
    except stderr is piped back to us and watched on a background thread
    instead of being thrown away.
    """
    import discord

    source = discord.FFmpegPCMAudio(
        url,
        before_options=before_options,
        options=options,
        stderr=subprocess.PIPE,
    )
    _watch_stderr(source, track_title)
    return source


def _watch_stderr(source, track_title: str):
    """
    ffmpeg's stderr comes through a real OS pipe, and reading it blocks,
    which is why this runs on its own daemon thread instead of inside
    the asyncio loop. Daemon means it never needs explicit teardown, it
    just dies along with the process it's reading from (or the bot).
    """
    process = getattr(source, "_process", None)
    if process is None or process.stderr is None:
        logger.debug(f"no ffmpeg stderr pipe available for '{track_title}', skipping stderr watch")
        return

    def _reader():
        try:
            for raw_line in iter(process.stderr.readline, b""):
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                lowered = line.lower()
                if "403" in line or "forbidden" in lowered:
                    logger.error(
                        f"[ffmpeg] '{track_title}' looks like it hit a 403/Forbidden "
                        f"from the stream host: {line}"
                    )
                elif "error" in lowered or "failed" in lowered:
                    logger.warning(f"[ffmpeg] '{track_title}': {line}")
                else:
                    logger.debug(f"[ffmpeg] '{track_title}': {line}")
        except Exception as e:
            # this thread reading a pipe should never realistically
            # blow up, but if it does it shouldn't take anything else
            # down with it
            logger.debug(f"ffmpeg stderr watcher for '{track_title}' died: {e}")

    threading.Thread(
        target=_reader,
        daemon=True,
        name=f"ffmpeg-stderr-{track_title[:20]}",
    ).start()
