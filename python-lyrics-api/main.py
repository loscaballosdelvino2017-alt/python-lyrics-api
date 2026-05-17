from fastapi import FastAPI, HTTPException
from ytmusicapi import YTMusic
from typing import Optional

app = FastAPI()
ytmusic = YTMusic()

def format_lrc_time(ms: int) -> str:
    """Format milliseconds into LRC [mm:ss.xx]"""
    try:
        total_seconds = ms / 1000.0
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)
        hundredths = int((total_seconds % 1) * 100)
        return f"[{minutes:02d}:{seconds:02d}.{hundredths:02d}]"
    except Exception:
        return ""

@app.get("/lyrics")
def get_lyrics(artist: str, title: str):
    query = f"{artist} {title}"
    try:
        # 1. Buscamos la canción
        search_results = ytmusic.search(query, filter="songs")
        if not search_results:
            return {"found": False, "error": "Song not found"}
        
        video_id = search_results[0]['videoId']
        
        # 2. Obtenemos los detalles de reproducción para sacar el ID de las letras
        watch_playlist = ytmusic.get_watch_playlist(videoId=video_id)
        lyrics_id = watch_playlist.get("lyrics")
        
        if not lyrics_id:
            return {"found": False, "error": "No lyrics available for this song in YT Music"}
            
        # 3. Descargamos las letras con tiempos sincronizados
        lyrics_data = ytmusic.get_lyrics(browseId=lyrics_id, timestamps=True)
        
        if not lyrics_data or 'lyrics' not in lyrics_data:
            return {"found": False, "error": "Lyrics data is empty"}

        lyrics_content = lyrics_data['lyrics']
        
        # Si devuelve un texto plano porque no hay sincronización
        if isinstance(lyrics_content, str):
            return {
                "found": True,
                "lyrics": lyrics_content,
                "source": "ytmusic",
                "synced": False
            }
            
        # Si devuelve formato sincronizado (array de objetos)
        if isinstance(lyrics_content, list):
            lrc_lines = []
            for line in lyrics_content:
                # Cada línea es un objeto en Python o un dict, lo parseamos
                text = getattr(line, 'text', '') if not isinstance(line, dict) else line.get('text', '')
                # Si el texto es solo un carácter musical, lo podemos omitir o dejar
                if text.strip() == '♪':
                    text = '♪'
                    
                start_ms = getattr(line, 'start_time', getattr(line, 'start', None)) if not isinstance(line, dict) else line.get('start_time', line.get('start'))
                
                if start_ms is not None:
                    try:
                        ms = int(start_ms)
                        time_str = format_lrc_time(ms)
                        lrc_lines.append(f"{time_str} {text}")
                    except ValueError:
                        lrc_lines.append(text)
                else:
                    lrc_lines.append(text)
            
            return {
                "found": True,
                "lyrics": "\n".join(lrc_lines),
                "source": "ytmusic",
                "synced": True
            }
            
        return {"found": False, "error": "Unknown lyrics format"}
        
    except Exception as e:
        return {"found": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    # Corre el servidor en el puerto 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
