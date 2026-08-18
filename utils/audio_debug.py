"""
Wraps discord.FFmpegPCMAudio so we can see what ffmpeg is actually
saying on stderr instead of it disappearing into devnull, which is
what discord.py does with it by default.

This exists because of the occasional 403 on stream urls. Without
this, a track that hits a 403 just goes silent (ffmpeg exits, the
after= callback fires like the song ended normally) with nothing in
our logs pointing at why. With this, the actual ffmpeg error line
(HTTP error 403 Forbidden, connection refused, whatever it turns out
to be) gets logged against the track that hit it, and an optional
on_forbidden callback lets the caller do something about it besides
just logging, like telling the user their song just died.

Important detail, learned the hard way: passing stderr=subprocess.PIPE
straight into discord.FFmpegPCMAudio does nothing on this version.
discord.py's FFmpegAudio explicitly checks for exactly that value and
throws it away with a deprecation warning, falling back to inherited
stderr, which is why ffmpeg's own error lines were landing straight in
the systemd journal under ffmpeg's pid instead of going anywhere we
could read them. What it wants instead is a plain file-like object
with no working fileno(), it uses that (specifically, the absence of
a usable fileno()) to decide "this isn't a real OS file descriptor, so
open a real pipe myself and hand this object the bytes via .write()".
_StderrSink below is exactly that, a write() target with no fileno,
so discord.py does its own pipe plumbing and just calls into us with
the chunks. We don't manage the ffmpeg process or its pipe at all
anymore, discord.py's own background thread does that and calls us.
"""
from utils.logger import get_logger

logger = get_logger(__name__)


class _StderrSink:
    """
    Minimal write() target for discord.py's own stderr-piping thread to
    push ffmpeg's stderr bytes into. Deliberately has no fileno(), see
    the module docstring for why that matters.

    ffmpeg doesn't line-buffer neatly across whatever chunk size
    discord.py's reader thread happens to read, so this just
    accumulates bytes and splits complete lines out of the buffer as
    they show up, same as reading a real pipe line by line would.
    """

    def __init__(self, on_line, track_title: str):
        self._buffer = b""
        self._on_line = on_line
        self._track_title = track_title

    def write(self, data: bytes) -> int:
        self._buffer += data
        while b"\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split(b"\n", 1)
            line = raw_line.decode(errors="replace").strip()
            if line:
                try:
                    self._on_line(line)
                except Exception as e:
                    logger.debug(f"error handling ffmpeg stderr line for '{self._track_title}': {e}")
        return len(data)

    def flush(self):
        # discord.py doesn't call this today, kept around since
        # file-like objects are generally expected to have it
        pass


def build_logged_ffmpeg_source(url: str, before_options: str, options: str, track_title: str, on_forbidden=None):
    """
    Same as discord.FFmpegPCMAudio(url, before_options=..., options=...),
    except ffmpeg's stderr actually gets routed to us and logged against
    the track, instead of vanishing or (as it turned out) leaking
    straight into the systemd journal unfiltered. on_forbidden, if
    given, gets called (no args) the first time a 403/Forbidden line
    shows up on that stream.
    """
    import discord

    # only fire the callback once per source, ffmpeg can print more
    # than one line mentioning 403 for the same underlying failure and
    # nobody needs to hear about it twice
    already_notified = [False]

    def _handle_line(line: str):
        lowered = line.lower()
        if "403" in line or "forbidden" in lowered:
            logger.error(f"[ffmpeg] '{track_title}' looks like it hit a 403/Forbidden from the stream host: {line}")
            if on_forbidden and not already_notified[0]:
                already_notified[0] = True
                try:
                    on_forbidden()
                except Exception as cb_err:
                    logger.debug(f"on_forbidden callback for '{track_title}' raised: {cb_err}")
        elif "error" in lowered or "failed" in lowered:
            logger.warning(f"[ffmpeg] '{track_title}': {line}")
        else:
            logger.debug(f"[ffmpeg] '{track_title}': {line}")

    sink = _StderrSink(_handle_line, track_title)

    return discord.FFmpegPCMAudio(
        url,
        before_options=before_options,
        options=options,
        stderr=sink,
    )
