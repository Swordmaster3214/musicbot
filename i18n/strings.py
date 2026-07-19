"""
All user facing text lives here, split by language code. Adding a
third language later just means adding one more dict below and making
sure every key that exists in "en" also exists there.

Everything else in the bot should go through t(key, lang, **kwargs)
instead of hardcoding strings, that's what keeps this the single
source of truth for what gets shown to a server.
"""

STRINGS = {
    "en": {
        # errors / validation
        "err_not_in_voice": "You need to be in a voice channel first.",
        "err_no_results_query": "No results found for that query.",
        "err_no_results_playlist": "No results found for that playlist.",
        "err_max_queue": "That would exceed the max queue size of {max}.",
        "err_play_generic": "An error occurred while processing your request: {error}",
        "err_shuffleplay_generic": "❌ An error occurred while processing your playlist: {error}",

        # queueing
        "queued_single": "Queued **{title}**.",
        "queued_playlist": "Queued **{count}** tracks from the playlist.",
        "queued_shuffled": "Queued and shuffled **{count}** tracks.",
        "queue_shuffled": "Queue shuffled.",
        "queue_cleared": "Cleared {count} track(s) from the queue.",
        "remove_nothing": "Nothing at that position.",
        "removed_track": "Removed **{title}** from the queue.",

        # playback control
        "paused": "Paused.",
        "nothing_playing": "Nothing is playing.",
        "resumed": "Resumed.",
        "nothing_paused": "Nothing is paused.",
        "skipped": "Skipped.",
        "seeked_forward": "Jumped ahead {seconds} seconds.",
        "seeked_back": "Rewound {seconds} seconds.",
        "stopped_cleared": "Stopped and cleared the queue.",
        "disconnected": "Disconnected.",
        "nothing_playing_now": "Nothing is playing right now.",

        # loop toggles
        "track_loop_state": "Track loop {state}.",
        "queue_loop_state": "Queue loop {state}.",
        "state_enabled": "enabled",
        "state_disabled": "disabled",
        "autoplay_state":  "Autoplay (24/7) mode {state}.",

        # cache search
        "no_cached_matches": "No cached songs matched that search.",

        # language command
        "language_set": "Language set to **{language}**.",

        # now playing / queue embeds
        "embed_now_playing_title": "Now Playing",
        "embed_artist": "Artist",
        "embed_duration": "Duration",
        "embed_source": "Source",
        "duration_live_unknown": "Live/Unknown",
        "embed_queue_title": "Queue",
        "embed_queue_empty": "The queue is empty.",
        "embed_queue_footer": "{count} track(s) total, page {page}",
        "source_direct": "Direct Link",

        # now playing view button labels
        "btn_seek_back": "Rewind 10s",
        "btn_pause_resume": "Pause/Resume",
        "btn_seek_forward": "Skip forward 10s",
        "btn_skip_song": "Skip song",
        "btn_shuffle": "Shuffle",
        "btn_loop_track": "Loop Track",
        "btn_loop_queue": "Loop Queue",
        "btn_stop": "Stop",
        "btn_autoplay": "Autoplay",

        # button-triggered variants
        "seeked_back_by": "{user} rewound {seconds} seconds.",
        "resumed_by": "{user} resumed playback.",
        "paused_by": "{user} paused playback.",
        "seeked_forward_by": "{user} jumped ahead {seconds} seconds.",
        "skipped_by": "{user} skipped the song.",
        "queue_shuffled_by": "{user} shuffled the queue.",
        "track_loop_state_by": "{user} turned track loop {state}.",
        "queue_loop_state_by": "{user} turned queue loop {state}.",
        "autoplay_state_by": "{user} turned autoplay (24/7) mode {state}.",
        "stopped_cleared_by": "{user} stopped playback and cleared the queue.",
    },
    "es": {
        # errors / validation
        "err_not_in_voice": "Tienes que estar en un canal de voz primero.",
        "err_no_results_query": "No se encontraron resultados para esa búsqueda.",
        "err_no_results_playlist": "No se encontraron resultados para esa lista.",
        "err_max_queue": "Eso superaría el tamaño máximo de la cola ({max}).",
        "err_play_generic": "Ocurrió un error al procesar tu solicitud: {error}",
        "err_shuffleplay_generic": "❌ Ocurrió un error al procesar tu lista: {error}",

        # queueing
        "queued_single": "Se añadió **{title}** a la cola.",
        "queued_playlist": "Se añadieron **{count}** canciones de la lista a la cola.",
        "queued_shuffled": "Se añadieron y mezclaron **{count}** canciones.",
        "queue_shuffled": "Cola mezclada.",
        "queue_cleared": "Se eliminaron {count} canción(es) de la cola.",
        "remove_nothing": "No hay nada en esa posición.",
        "removed_track": "Se eliminó **{title}** de la cola.",

        # playback control
        "paused": "Pausado.",
        "nothing_playing": "No se está reproduciendo nada.",
        "resumed": "Reanudado.",
        "nothing_paused": "No hay nada en pausa.",
        "skipped": "Canción saltada.",
        "seeked_forward": "Avanzó {seconds} segundos.",
        "seeked_back": "Retrocedió {seconds} segundos.",
        "stopped_cleared": "Se detuvo la reproducción y se vació la cola.",
        "disconnected": "Desconectado.",
        "nothing_playing_now": "No hay nada reproduciéndose en este momento.",

        # loop toggles
        "track_loop_state": "Bucle de canción {state}.",
        "queue_loop_state": "Bucle de cola {state}.",
        "state_enabled": "activado",
        "state_disabled": "desactivado",
        "autoplay_state": "Modo de reproducción automática (24/7) {state}.",

        # cache search
        "no_cached_matches": "Ninguna canción en caché coincide con esa búsqueda.",

        # language command
        "language_set": "Idioma establecido en **{language}**.",

        # now playing / queue embeds
        "embed_now_playing_title": "Reproduciendo ahora",
        "embed_artist": "Artista",
        "embed_duration": "Duración",
        "embed_source": "Fuente",
        "duration_live_unknown": "En vivo/Desconocido",
        "embed_queue_title": "Cola",
        "embed_queue_empty": "La cola está vacía.",
        "embed_queue_footer": "{count} canción(es) en total, página {page}",
        "source_direct": "Enlace directo",

        # now playing view button labels
        "btn_seek_back": "Retroceder 10s",
        "btn_pause_resume": "Pausar/Reanudar",
        "btn_seek_forward": "Avanzar 10s",
        "btn_skip_song": "Saltar canción",
        "btn_shuffle": "Mezclar",
        "btn_loop_track": "Repetir canción",
        "btn_loop_queue": "Repetir cola",
        "btn_stop": "Detener",
        "btn_autoplay": "Reproducción automática",

        "seeked_back_by": "{user} retrocedió {seconds} segundos.",
        "resumed_by": "{user} reanudó la reproducción.",
        "paused_by": "{user} pausó la reproducción.",
        "seeked_forward_by": "{user} avanzó {seconds} segundos.",
        "skipped_by": "{user} saltó la canción.",
        "queue_shuffled_by": "{user} mezcló la cola.",
        "track_loop_state_by": "{user} puso el bucle de canción en {state}.",
        "queue_loop_state_by": "{user} puso el bucle de cola en {state}.",
        "autoplay_state_by": "{user} puso el modo de reproducción automática (24/7) en {state}.",
        "stopped_cleared_by": "{user} detuvo la reproducción y vació la cola.",
    },
}

DEFAULT_LANG = "en"

# language codes we actually support, used by the /language command
# choices and by anything that needs to validate a stored value
SUPPORTED_LANGUAGES = {"en": "English", "es": "Español"}


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """
    Looks up a string by key for the given language, falling back to
    English if the language or the key is missing, and finally
    falling back to the raw key itself so a typo shows up as an odd
    string in Discord instead of crashing a command.
    """
    lang_dict = STRINGS.get(lang, STRINGS[DEFAULT_LANG])
    template = lang_dict.get(key, STRINGS[DEFAULT_LANG].get(key, key))

    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        # a format placeholder didn't get filled in, better to show
        # the unformatted template than to blow up the command
        return template
