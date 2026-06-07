from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
from typing import Optional
import yt_dlp
import requests
import math
import subprocess
import numpy as np
import imageio_ffmpeg

app = FastAPI()

# Permitir CORS para peticiones desde el navegador (necesario para el test en HTML)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ytmusic = YTMusic()

def get_youtube_audio_info(video_id: str):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': ['player_client=android']
        }
    }

    import os
    if os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = 'cookies.txt'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        audio_url = info.get('url')
        if not audio_url:
            raise HTTPException(status_code=404, detail="Audio URL not found")
        return {
            "audio_url": audio_url,
            "duration": float(info.get("duration") or 0),
            "title": info.get("title") or video_id,
        }

def decode_audio_from_url(audio_url: str, max_duration: float = 240.0, sample_rate: int = 22050):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-headers",
        "User-Agent: Mozilla/5.0\r\n",
        "-i",
        audio_url,
        "-t",
        str(max_duration),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100)
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "ignore")[-500:] or "ffmpeg produced no audio"
        raise HTTPException(status_code=502, detail=detail)

    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size < sample_rate:
        raise HTTPException(status_code=422, detail="Decoded audio is too short")
    audio = np.nan_to_num(audio)
    peak = float(np.max(np.abs(audio))) or 1.0
    return audio / peak, sample_rate

def moving_average(values, width: int):
    if width <= 1:
        return values
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(values, kernel, mode="same")

def local_maxima(values, threshold: float, min_distance: int):
    peaks = []
    last = -min_distance
    for index in range(1, len(values) - 1):
        if index - last < min_distance:
            continue
        if values[index] >= threshold and values[index] >= values[index - 1] and values[index] > values[index + 1]:
            peaks.append(index)
            last = index
    return peaks

def estimate_bpm(onset_env, hop_time: float):
    centered = onset_env - float(np.mean(onset_env))
    if len(centered) < 8 or float(np.max(np.abs(centered))) <= 1e-6:
        return 0.0

    corr = np.correlate(centered, centered, mode="full")[len(centered)-1:]
    best_bpm = 0.0
    best_score = -1.0
    for bpm in np.arange(70, 181, 0.5):
        lag = int(round(60.0 / (bpm * hop_time)))
        if 1 <= lag < len(corr) and corr[lag] > best_score:
            best_score = float(corr[lag])
            best_bpm = float(bpm)
    return best_bpm

def analyze_waveform(audio, sample_rate: int, source_duration: float):
    frame = 2048
    hop = 512
    if len(audio) < frame:
        raise HTTPException(status_code=422, detail="Audio is too short for rhythm analysis")

    frame_count = 1 + (len(audio) - frame) // hop
    rms = np.empty(frame_count, dtype=np.float32)
    for i in range(frame_count):
        chunk = audio[i * hop:i * hop + frame]
        rms[i] = math.sqrt(float(np.mean(chunk * chunk)))

    rms = moving_average(rms, 5)
    if float(np.max(rms)) > 0:
        energy = rms / float(np.max(rms))
    else:
        energy = rms

    onset = np.maximum(0, np.diff(energy, prepend=energy[0]))
    onset = moving_average(onset, 3)
    if float(np.max(onset)) > 0:
        onset_norm = onset / float(np.max(onset))
    else:
        onset_norm = onset

    hop_time = hop / sample_rate
    duration = source_duration if source_duration > 0 else len(audio) / sample_rate
    analyzed_duration = min(duration, len(audio) / sample_rate)
    bpm = estimate_bpm(onset_norm, hop_time)

    threshold = float(np.mean(onset_norm) + np.std(onset_norm) * 0.85)
    peak_indexes = local_maxima(onset_norm, threshold, max(1, int(0.18 / hop_time)))
    peak_times = [idx * hop_time for idx in peak_indexes if idx * hop_time <= analyzed_duration]

    points = []
    if bpm > 0:
        beat_period = 60.0 / bpm
        beat_time = peak_times[0] if peak_times else beat_period
        beat_index = 0
        while beat_time < analyzed_duration:
            frame_index = min(len(energy) - 1, max(0, int(beat_time / hop_time)))
            intensity = 0.35 + 0.65 * float(energy[frame_index])
            points.append({
                "time": round(float(beat_time), 3),
                "intensity": round(float(min(1.0, max(0.08, intensity))), 4),
                "important": beat_index % 4 == 0 or intensity > 0.78,
            })
            beat_time += beat_period
            beat_index += 1

    for time in peak_times[:350]:
        frame_index = min(len(energy) - 1, max(0, int(time / hop_time)))
        intensity = max(float(onset_norm[frame_index]), float(energy[frame_index]))
        if intensity > 0.58:
            points.append({
                "time": round(float(time), 3),
                "intensity": round(float(min(1.0, max(0.08, intensity))), 4),
                "important": intensity > 0.78,
            })

    points_by_time = {}
    for point in points:
        bucket = round(point["time"] * 2) / 2
        current = points_by_time.get(bucket)
        if not current or point["intensity"] > current["intensity"]:
            points_by_time[bucket] = {**point, "time": bucket}
    points = sorted(points_by_time.values(), key=lambda item: item["time"])[:650]

    section_length = 16.0
    sections = []
    cursor = 0.0
    labels = ["intro", "verse", "build", "drop", "break", "chorus", "outro"]
    while cursor < analyzed_duration:
        end = min(analyzed_duration, cursor + section_length)
        start_frame = int(cursor / hop_time)
        end_frame = max(start_frame + 1, int(end / hop_time))
        avg_energy = float(np.mean(energy[start_frame:min(end_frame, len(energy))]))
        if cursor < 8:
            label = "intro"
        elif end > analyzed_duration - 12:
            label = "outro"
        elif avg_energy > 0.68:
            label = "drop"
        elif avg_energy > 0.48:
            label = "chorus"
        elif avg_energy < 0.24:
            label = "break"
        else:
            label = labels[len(sections) % len(labels)]
        sections.append({
            "start": round(cursor, 3),
            "end": round(end, 3),
            "label": label,
            "kind": label,
        })
        cursor = end

    if not points:
        for t in np.arange(1.0, analyzed_duration, 1.5):
            points.append({"time": round(float(t), 3), "intensity": 0.5, "important": int(t) % 6 == 0})

    return {
        "found": True,
        "source": "python-rhythm-analysis",
        "duration": round(float(duration), 3),
        "analyzedDuration": round(float(analyzed_duration), 3),
        "bpm": round(float(bpm), 2),
        "sections": sections,
        "points": points,
    }

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

def stream_youtube_audio(video_id: str):
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'extractor_args': {
                'youtube': ['player_client=android']
            }
        }
        
        import os
        if os.path.exists("cookies.txt"):
            ydl_opts['cookiefile'] = 'cookies.txt'
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            audio_url = info.get('url')
            if not audio_url:
                raise HTTPException(status_code=404, detail="Audio URL not found")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            r = requests.get(audio_url, headers=headers, stream=True)
            if not r.ok:
                raise HTTPException(status_code=r.status_code, detail="Failed to fetch audio stream")
                
            def generate_stream():
                for chunk in r.iter_content(chunk_size=512 * 1024):
                    yield chunk
                    
            return StreamingResponse(
                generate_stream(),
                media_type="audio/mpeg"
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download_audio")
def download_audio(video_id: str):
    return stream_youtube_audio(video_id)

@app.get("/download_audio/{video_id}.mp3")
def download_audio_mp3(video_id: str):
    return stream_youtube_audio(video_id)

@app.get("/download_audio/{video_id}")
def download_audio_path(video_id: str):
    return stream_youtube_audio(video_id.removesuffix(".mp3"))

@app.get("/audio/{video_id}.mp3")
def audio_mp3(video_id: str):
    return stream_youtube_audio(video_id)

@app.get("/rhythm_analysis")
def rhythm_analysis(video_id: str, max_duration: Optional[float] = 240.0):
    try:
        info = get_youtube_audio_info(video_id)
        audio, sample_rate = decode_audio_from_url(
            info["audio_url"],
            max_duration=max(30.0, min(float(max_duration or 240.0), 360.0)),
        )
        result = analyze_waveform(audio, sample_rate, info["duration"])
        result["title"] = info["title"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/rhythm_analysis/{video_id}")
def rhythm_analysis_path(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id.removesuffix(".mp3"), max_duration)

if __name__ == "__main__":
    import uvicorn
    # Corre el servidor en el puerto 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
