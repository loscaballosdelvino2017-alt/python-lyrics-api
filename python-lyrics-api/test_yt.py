from ytmusicapi import YTMusic
import json

ytmusic = YTMusic()
search_results = ytmusic.search("The Weeknd Blinding Lights", filter="songs")
video_id = search_results[0]['videoId']
watch_playlist = ytmusic.get_watch_playlist(videoId=video_id)
lyrics_id = watch_playlist.get("lyrics")

print("Lyrics ID:", lyrics_id)

try:
    lyrics_data = ytmusic.get_lyrics(lyrics_id, timestamps=True)
    # the returned data might have custom python objects, let's just print attributes
    if 'lyrics' in lyrics_data and not isinstance(lyrics_data['lyrics'], str):
        # Could be an object with lines
        lines = []
        for line in lyrics_data['lyrics']:
            lines.append({
                "text": getattr(line, "text", str(line)),
                "start": getattr(line, "start_time", getattr(line, "start", "")),
                "end": getattr(line, "end_time", getattr(line, "end", ""))
            })
        print(json.dumps(lines, indent=2))
    else:
        print(lyrics_data)
except Exception as e:
    print("Error:", e)
