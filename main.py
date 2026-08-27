from fastapi import FastAPI, HTTPException, Header
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
import os
import tempfile
import shutil

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
CYANITE_GRAPHQL_URL = "https://api.cyanite.ai/graphql"

def get_youtube_audio_info(video_id: str):

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': ['player_client=android', 'player_skip=webpage']
        }
    }

    try:
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
    except Exception as e:
        import requests
        
        # 1. Fallback: Cobalt (Instancia pública si existe, o cambiar por tu propio host)
        try:
            cobalt_host = "https://co.wuk.sh" # Reemplazar con una instancia viva de Cobalt
            req = requests.post(f"{cobalt_host}/api/json", json={"url": f"https://www.youtube.com/watch?v={video_id}", "isAudioOnly": True, "aFormat": "mp3"}, timeout=5)
            if req.status_code == 200 and req.json().get("url"):
                return {"audio_url": req.json()["url"], "duration": 0, "title": video_id}
        except Exception:
            pass

        # 2. Fallback: Invidious (Prueba un par de instancias públicas conocidas)
        invidious_instances = ["https://inv.tux.pizza", "https://invidious.nerdvpn.de", "https://invidious.slipfox.xyz"]
        for inv_host in invidious_instances:
            try:
                res = requests.get(f"{inv_host}/api/v1/videos/{video_id}", timeout=5)
                data = res.json()
                if data.get("formatStreams"):
                    audio_streams = [s for s in data["formatStreams"] if s["type"].startswith("audio")]
                    if audio_streams:
                        return {"audio_url": audio_streams[0]["url"], "duration": float(data.get("lengthSeconds") or 0), "title": data.get("title") or video_id}
            except Exception:
                continue

        # 3. Fallback final: RapidAPI
        try:
            rapid_api_key = "ca2070ca95msh581ae5a2dbb312dp11a994jsnecb211fa4b48"
            headers = {"X-RapidAPI-Key": rapid_api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
            resp = requests.get(f"https://youtube-mp36.p.rapidapi.com/dl?id={video_id}", headers=headers, timeout=10)
            data = resp.json()
            if data.get("status") == "ok" and data.get("link"):
                return {"audio_url": data["link"], "duration": float(data.get("duration") or 0), "title": data.get("title") or video_id}
        except Exception:
            pass
            
        raise e

def decode_audio_from_url(audio_url: str, max_duration: float = 240.0, sample_rate: int = 22050):
    return decode_audio_source(audio_url, max_duration=max_duration, sample_rate=sample_rate, is_url=True)

def decode_audio_source(source: str, max_duration: float = 240.0, sample_rate: int = 22050, is_url: bool = False):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if is_url:
        command.extend([
            "-headers",
            "User-Agent: Mozilla/5.0\r\nReferer: https://www.youtube.com/\r\n",
        ])
    command.extend([
        "-i",
        source,
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
    ])
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

def download_youtube_audio_file(video_id: str):
    temp_dir = tempfile.mkdtemp(prefix="yt_audio_")
    outtmpl = os.path.join(temp_dir, "audio.%(ext)s")
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'noplaylist': True,
        'extractor_args': {
            'youtube': ['player_client=android', 'player_skip=webpage']
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        files = [os.path.join(temp_dir, name) for name in os.listdir(temp_dir)]
        audio_files = [path for path in files if os.path.isfile(path)]
        if not audio_files:
            raise HTTPException(status_code=502, detail="yt-dlp downloaded no audio file")
        return {
            "path": audio_files[0],
            "temp_dir": temp_dir,
            "duration": float(info.get("duration") or 0),
            "title": info.get("title") or video_id,
        }
    except Exception as e:
        import requests
        
        def download_and_return(url, dur, title):
            mp3_path = os.path.join(temp_dir, "audio.mp3")
            r = requests.get(url, stream=True, timeout=20)
            r.raise_for_status()
            with open(mp3_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return {"path": mp3_path, "temp_dir": temp_dir, "duration": dur, "title": title}

        # 1. Fallback: Cobalt
        try:
            req = requests.post("https://co.wuk.sh/api/json", json={"url": f"https://www.youtube.com/watch?v={video_id}", "isAudioOnly": True, "aFormat": "mp3"}, timeout=5)
            if req.status_code == 200 and req.json().get("url"):
                return download_and_return(req.json()["url"], 0, video_id)
        except Exception:
            pass

        # 2. Fallback: Invidious
        invidious_instances = ["https://inv.tux.pizza", "https://invidious.nerdvpn.de", "https://invidious.slipfox.xyz"]
        for inv_host in invidious_instances:
            try:
                res = requests.get(f"{inv_host}/api/v1/videos/{video_id}", timeout=5)
                data = res.json()
                if data.get("formatStreams"):
                    audio_streams = [s for s in data["formatStreams"] if s["type"].startswith("audio")]
                    if audio_streams:
                        return download_and_return(audio_streams[0]["url"], float(data.get("lengthSeconds") or 0), data.get("title") or video_id)
            except Exception:
                continue

        # 3. Fallback: RapidAPI
        try:
            rapid_api_key = "ca2070ca95msh581ae5a2dbb312dp11a994jsnecb211fa4b48"
            headers = {"X-RapidAPI-Key": rapid_api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
            resp = requests.get(f"https://youtube-mp36.p.rapidapi.com/dl?id={video_id}", headers=headers, timeout=10)
            data = resp.json()
            if data.get("status") == "ok" and data.get("link"):
                return download_and_return(data["link"], float(data.get("duration") or 0), data.get("title") or video_id)
        except Exception:
            pass
            
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise e

def convert_to_mp3(source_path: str, temp_dir: str, max_duration: float = 240.0):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mp3_path = os.path.join(temp_dir, "cyanite-upload.mp3")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_path,
        "-t",
        str(max_duration),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-b:a",
        "160k",
        "-y",
        mp3_path,
    ]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 10000:
        detail = proc.stderr.decode("utf-8", "ignore")[-500:] or "ffmpeg did not create mp3"
        raise HTTPException(status_code=502, detail=detail)
    return mp3_path

def cyanite_graphql(token: str, query: str, variables=None):
    response = requests.post(
        CYANITE_GRAPHQL_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=45,
    )
    try:
        data = response.json()
    except Exception:
        raise HTTPException(status_code=response.status_code, detail=response.text[:500])
    if not response.ok or data.get("errors"):
        raise HTTPException(status_code=response.status_code, detail=data)
    return data.get("data") or {}

def cyanite_upload_mp3(token: str, mp3_path: str, title: str, external_id: str):
    upload_data = cyanite_graphql(
        token,
        """
        mutation FileUploadRequest {
          fileUploadRequest {
            id
            uploadUrl
          }
        }
        """,
    )
    upload = upload_data.get("fileUploadRequest") or {}
    upload_id = upload.get("id")
    upload_url = upload.get("uploadUrl")
    if not upload_id or not upload_url:
        raise HTTPException(status_code=502, detail=f"Cyanite did not return upload info: {upload_data}")

    with open(mp3_path, "rb") as handle:
        put_response = requests.put(
            upload_url,
            data=handle,
            headers={"Content-Type": "audio/mpeg"},
            timeout=180,
        )
    if not put_response.ok:
        raise HTTPException(status_code=put_response.status_code, detail=put_response.text[:500])

    create_data = cyanite_graphql(
        token,
        """
        mutation LibraryTrackCreate($input: LibraryTrackCreateInput!) {
          libraryTrackCreate(input: $input) {
            __typename
            ... on LibraryTrackCreateSuccess {
              createdLibraryTrack {
                id
                title
              }
            }
            ... on LibraryTrackCreateError {
              code
              message
            }
          }
        }
        """,
        {
            "input": {
                "uploadId": upload_id,
                "title": title[:150],
                "externalId": external_id[:150],
            }
        },
    )
    result = create_data.get("libraryTrackCreate") or {}
    if result.get("__typename") != "LibraryTrackCreateSuccess":
        raise HTTPException(status_code=502, detail=result)
    track = result.get("createdLibraryTrack") or {}
    track_id = track.get("id")
    if not track_id:
        raise HTTPException(status_code=502, detail=f"Cyanite did not return track id: {create_data}")
    return track_id

def cyanite_fetch_analysis(token: str, track_id: str):
    return cyanite_graphql(
        token,
        """
        query LibraryTrackAnalysis($id: ID!) {
          libraryTrack(id: $id) {
            __typename
            ... on LibraryTrack {
              id
              title
              audioAnalysisV6 {
                __typename
                ... on AudioAnalysisV6Finished {
                  result {
                    bpmPrediction {
                      value
                      confidence
                    }
                    bpmRangeAdjusted
                    valence
                    arousal
                    segments {
                      timestamps
                      valence
                      arousal
                      movement {
                        bouncy
                        driving
                        groovy
                        pulsing
                        steady
                        stomping
                      }
                      mood {
                        aggressive
                        calm
                        energetic
                        epic
                        happy
                        uplifting
                      }
                    }
                  }
                }
                ... on AudioAnalysisV6Failed {
                  __typename
                }
                ... on AudioAnalysisV6NotAuthorized {
                  __typename
                }
              }
            }
          }
        }
        """,
        {"id": track_id},
    )

def cyanite_to_level_data(data, fallback_duration: float):
    track = data.get("libraryTrack") or {}
    analysis = track.get("audioAnalysisV6") or {}
    typename = analysis.get("__typename")
    if typename != "AudioAnalysisV6Finished":
        if typename in ("AudioAnalysisV6Failed", "AudioAnalysisV6Error"):
            raise HTTPException(status_code=502, detail=analysis)
        return None

    result = analysis.get("result") or {}
    bpm = float((result.get("bpmPrediction") or {}).get("value") or result.get("bpmRangeAdjusted") or 0)
    segments_data = result.get("segments") or {}
    timestamps = segments_data.get("timestamps") or []
    arousal = segments_data.get("arousal") or []
    valence = segments_data.get("valence") or []
    movement = segments_data.get("movement") or {}
    duration = float(fallback_duration or 180)
    if timestamps:
        duration = max(duration, float(timestamps[-1]) + 8.0)

    points = []
    if bpm > 0:
        step = 60.0 / max(40.0, min(220.0, bpm))
        t = step
        index = 0
        while t < duration:
            points.append({
                "time": round(t, 3),
                "intensity": 0.75 if index % 4 == 0 else 0.48,
                "important": index % 4 == 0,
            })
            t += step
            index += 1

    energetic = movement.get("driving") or movement.get("pulsing") or movement.get("groovy") or []
    for index, start in enumerate(timestamps):
        energy = max(
            float(arousal[index]) if index < len(arousal) else 0.0,
            float(energetic[index]) if index < len(energetic) else 0.0,
        )
        if energy > 0.45:
            points.append({
                "time": round(float(start), 3),
                "intensity": round(max(0.08, min(1.0, energy)), 4),
                "important": energy > 0.7,
            })

    sections = []
    mood = segments_data.get("mood") or {}
    for index, start in enumerate(timestamps):
        end = float(timestamps[index + 1]) if index + 1 < len(timestamps) else min(duration, float(start) + 8.0)
        scores = {
            "drop": float(movement.get("driving", [0] * len(timestamps))[index]) if index < len(movement.get("driving", [])) else 0,
            "build": float(movement.get("pulsing", [0] * len(timestamps))[index]) if index < len(movement.get("pulsing", [])) else 0,
            "chorus": float(mood.get("energetic", [0] * len(timestamps))[index]) if index < len(mood.get("energetic", [])) else 0,
            "break": float(mood.get("calm", [0] * len(timestamps))[index]) if index < len(mood.get("calm", [])) else 0,
        }
        label = max(scores, key=scores.get) if scores else "section"
        sections.append({
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "label": label,
            "kind": label,
        })

    return {
        "found": True,
        "source": "cyanite-analysis",
        "duration": round(duration, 3),
        "bpm": round(bpm, 2),
        "sections": sections,
        "points": points,
        "trackId": track.get("id"),
    }

def run_cyanite_analysis(video_id: str, token: str, max_duration: float):
    downloaded = None
    try:
        downloaded = download_youtube_audio_file(video_id)
        mp3_path = convert_to_mp3(downloaded["path"], downloaded["temp_dir"], max_duration=max_duration)
        track_id = cyanite_upload_mp3(
            token,
            mp3_path,
            downloaded["title"] or video_id,
            f"youtube:{video_id}",
        )
        last_data = None
        import time
        for _ in range(18):
            last_data = cyanite_fetch_analysis(token, track_id)
            parsed = cyanite_to_level_data(last_data, downloaded["duration"])
            if parsed:
                return parsed
            time.sleep(5)
        raise HTTPException(status_code=504, detail={"message": "Cyanite analysis timed out", "last": last_data})
    finally:
        if downloaded and downloaded.get("temp_dir"):
            shutil.rmtree(downloaded["temp_dir"], ignore_errors=True)

def bearer_token(authorization: Optional[str]):
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value.split(" ", 1)[1].strip()
    return value

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
                'youtube': ['player_client=android', 'player_skip=webpage']
            }
        }
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            audio_url = info.get('url')
            if audio_url:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                r = requests.get(audio_url, headers=headers, stream=True, timeout=10)
                if r.ok:
                    def generate_stream():
                        for chunk in r.iter_content(chunk_size=512 * 1024): yield chunk
                    return StreamingResponse(generate_stream(), media_type="audio/mpeg")
            raise Exception("YT-dlp stream failed")
    except Exception as e:
        import requests
        
        # 1. Fallback: Cobalt
        try:
            req = requests.post("https://co.wuk.sh/api/json", json={"url": f"https://www.youtube.com/watch?v={video_id}", "isAudioOnly": True, "aFormat": "mp3"}, timeout=10)
            if req.status_code == 200 and req.json().get("url"):
                r = requests.get(req.json()["url"], stream=True, timeout=60)
                if r.ok:
                    def gen():
                        for c in r.iter_content(chunk_size=512*1024): yield c
                    return StreamingResponse(gen(), media_type="audio/mpeg")
        except Exception:
            pass

        # 2. Fallback: Invidious
        for inv_host in ["https://inv.tux.pizza", "https://invidious.nerdvpn.de", "https://invidious.slipfox.xyz"]:
            try:
                res = requests.get(f"{inv_host}/api/v1/videos/{video_id}", timeout=10)
                data = res.json()
                if data.get("formatStreams"):
                    audio_streams = [s for s in data["formatStreams"] if s["type"].startswith("audio")]
                    if audio_streams:
                        r = requests.get(audio_streams[0]["url"], stream=True, timeout=60)
                        if r.ok:
                            def gen():
                                for c in r.iter_content(chunk_size=512*1024): yield c
                            return StreamingResponse(gen(), media_type="audio/mpeg")
            except Exception:
                continue

        # 3. Fallback: RapidAPI
        try:
            rapid_api_key = "ca2070ca95msh581ae5a2dbb312dp11a994jsnecb211fa4b48"
            resp = requests.get(f"https://youtube-mp36.p.rapidapi.com/dl?id={video_id}", headers={"X-RapidAPI-Key": rapid_api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=20)
            data = resp.json()
            if data.get("status") == "ok" and data.get("link"):
                r = requests.get(data["link"], stream=True, timeout=60)
                if r.ok:
                    def gen():
                        for c in r.iter_content(chunk_size=512*1024): yield c
                    return StreamingResponse(gen(), media_type="audio/mpeg")
        except Exception:
            pass
            
        raise e

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
    downloaded = None
    try:
        limited_duration = max(30.0, min(float(max_duration or 240.0), 360.0))
        audio = None
        sample_rate = None
        info = {"duration": 0, "title": video_id}

        # Step 1: Try getting audio info via yt-dlp (+ its fallbacks)
        try:
            info = get_youtube_audio_info(video_id)
            audio, sample_rate = decode_audio_from_url(
                info["audio_url"],
                max_duration=limited_duration,
            )
        except Exception:
            pass  # Fall through to download method

        # Step 2: If streaming failed, try downloading the file (+ its RapidAPI fallback)
        if audio is None:
            downloaded = download_youtube_audio_file(video_id)
            info["duration"] = downloaded["duration"] or info["duration"]
            info["title"] = downloaded["title"] or info["title"]
            audio, sample_rate = decode_audio_source(
                downloaded["path"],
                max_duration=limited_duration,
                is_url=False,
            )

        result = analyze_waveform(audio, sample_rate, info["duration"])
        result["title"] = info["title"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if downloaded and downloaded.get("temp_dir"):
            shutil.rmtree(downloaded["temp_dir"], ignore_errors=True)

@app.get("/rhythm_analysis/{video_id}")
def rhythm_analysis_path(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id.removesuffix(".mp3"), max_duration)

@app.get("/analyze_rhythm")
def analyze_rhythm(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id, max_duration)

@app.get("/analyze_rhythm/{video_id}")
def analyze_rhythm_path(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id.removesuffix(".mp3"), max_duration)

@app.get("/rhythm-analysis")
def rhythm_analysis_dash(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id, max_duration)

@app.get("/api/rhythm_analysis")
def api_rhythm_analysis(video_id: str, max_duration: Optional[float] = 240.0):
    return rhythm_analysis(video_id, max_duration)

@app.get("/cyanite_analysis")
def cyanite_analysis(
    video_id: str,
    max_duration: Optional[float] = 240.0,
    authorization: Optional[str] = Header(default=None),
):
    token = bearer_token(authorization) or os.getenv("CYANITE_ACCESS_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing Cyanite access token")
    limited_duration = max(30.0, min(float(max_duration or 240.0), 900.0))
    return run_cyanite_analysis(video_id, token, limited_duration)

@app.get("/cyanite_analysis/{video_id}")
def cyanite_analysis_path(
    video_id: str,
    max_duration: Optional[float] = 240.0,
    authorization: Optional[str] = Header(default=None),
):
    return cyanite_analysis(video_id.removesuffix(".mp3"), max_duration, authorization)

@app.get("/api/cyanite_analysis")
def api_cyanite_analysis(
    video_id: str,
    max_duration: Optional[float] = 240.0,
    authorization: Optional[str] = Header(default=None),
):
    return cyanite_analysis(video_id, max_duration, authorization)

@app.get("/health")
def health():
    return {"ok": True, "service": "python-lyrics-api", "rhythmAnalysis": True, "cyaniteAnalysis": True}

if __name__ == "__main__":
    import uvicorn
    # Corre el servidor en el puerto 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
