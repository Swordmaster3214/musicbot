# test_clients.py - run from project root after activating venv
import yt_dlp

# Testing URL provided for extraction
TEST_URL = "https://www.youtube.com/watch?v=87LBOCbbsAY"

# Storing clients and their limitations from comments inside a dictionary
CLIENTS_WITH_LIMITATIONS = {
    'default': '',
    'web': '',
    # Safari UA returns pre-merged video+audio 144p/240p/360p/720p/1080p HLS formats
    'web_safari': 'Safari UA returns pre-merged video+audio 144p/240p/360p/720p/1080p HLS formats',
    'web_embedded': '',
    'web_music': '',
    'web_creator': '',
    'android': '',
    # "Made for kids" videos aren't available with this client
    # Using a clientVersion>1.65 may return SABR streams only
    'android_vr': '"Made for kids" videos aren\'t available. Using a clientVersion>1.65 may return SABR streams only',
    # iOS clients have HLS live streams. Setting device model to get 60fps formats.
    # See: https://github.com/TeamNewPipe/NewPipeExtractor/issues/680#issuecomment-1002724558
    'ios': 'iOS clients have HLS live streams. Setting device model to get 60fps formats.',
    # "Made for kids" videos aren't available with this client
    'visionos': '"Made for kids" videos aren\'t available with this client',
    # mweb has 'ultralow' formats
    # See: https://github.com/yt-dlp/yt-dlp/pull/557
    'mweb': "mweb has 'ultralow' formats",
    'tv': '',
    'tv_downgraded': '',
    'tv_simply': '',
}

successful_clients = []

for client, limitations in CLIENTS_WITH_LIMITATIONS.items():
    print(f"\n{'='*60}")
    print(f"Testing client: {client}")
    print(f"{'='*60}")

    opts = {
        "format": "bestaudio",
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 10,
        "retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": [client]
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(TEST_URL, download=False)
            if info and info.get("format"):
                fmt = info["format"]
                abr = info.get('abr')

                # Default abr to 0 if it is missing or None to allow correct sorting numerical order
                abr_numeric = abr if isinstance(abr, (int, float)) else 0

                successful_clients.append({
                    'client': client,
                    'abr': abr_numeric,
                    'format': fmt,
                    'format_id': info.get('format_id'),
                    'ext': info.get('ext'),
                    'filesize': info.get('filesize', 'unknown'),
                    'limitations': limitations
                })

                print(f"Format: {fmt}")
                print(f"Format ID: {info.get('format_id')}")
                print(f"Ext: {info.get('ext')}")
                print(f"Abr: {abr} (audio bitrate)")
                print(f"Filesize: {info.get('filesize', 'unknown')}")
                print(f"SUCCESS")
            else:
                print("No format info returned")
    except Exception as e:
        print(f"FAILED: {e}")

# Sort the clients from highest bitrate to lowest bitrate
successful_clients.sort(key=lambda x: x['abr'], reverse=True)

# Generate the ranked summary report
print(f"\n\n{'='*60}")
print(f"{' '*14}RANKED FUNCTIONING CLIENTS REPORT")
print(f"{'='*60}")

if not successful_clients:
    print("No clients successfully returned video audio formats.")
else:
    for rank, client_info in enumerate(successful_clients, start=1):
        print(f"Rank {rank}: {client_info['client']}")
        print(f"  - Audio Bitrate (abr): {client_info['abr']} kbps")
        print(f"  - Format details: {client_info['format_id']} ({client_info['ext']})")
        print(f"  - Filesize: {client_info['filesize']}")
        if client_info['limitations']:
            print(f"  - Client Limitations: {client_info['limitations']}")
        print(f"{'-'*60}")
