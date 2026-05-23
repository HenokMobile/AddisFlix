import os
import uuid
import subprocess
import tempfile
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import aiofiles
import whisper
from deep_translator import GoogleTranslator
from gtts import gTTS

app = FastAPI(title="AddisFlix Synchronized Video Dubbing")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

whisper_model = None

def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        print("Whisper ሞዴል እየጫነ...")
        whisper_model = whisper.load_model("base")
    return whisper_model

SUPPORTED_LANGUAGES = {
    "am": "አማርኛ",
    "en": "English",
    "ar": "العربية",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
    "ru": "Русский",
    "zh-CN": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "tr": "Türkçe",
    "sw": "Kiswahili",
    "so": "Soomaali",
    "ha": "Hausa",
    "yo": "Yorùbá",
    "om": "Afaan Oromoo",
}

@app.get("/api/languages")
async def get_languages():
    return {"languages": SUPPORTED_LANGUAGES}


def get_audio_duration(path: Path) -> float:
    """FFprobe ተጠቅሞ የድምፅ ፋይል ርዝምና ያወጣል"""
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ], capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def build_atempo_filter(ratio: float) -> str:
    """
    ffmpeg atempo 0.5-2.0 ብቻ ይቀበላል።
    ከ2.0 በላይ ወይም ከ0.5 በታች ሲሆን filter ይደረደራሉ።
    """
    filters = []
    if ratio > 2.0:
        while ratio > 2.0:
            filters.append("atempo=2.0")
            ratio /= 2.0
        filters.append(f"atempo={ratio:.4f}")
    elif ratio < 0.5:
        while ratio < 0.5:
            filters.append("atempo=0.5")
            ratio /= 0.5
        filters.append(f"atempo={ratio:.4f}")
    else:
        filters.append(f"atempo={ratio:.4f}")
    return ",".join(filters)


def adjust_tts_speed(tts_path: Path, target_duration: float, out_path: Path):
    """
    TTS ድምፅ ርዝምና ከቪዲዮ segment duration ጋር ያስተካክላል።
    - TTS ረዘም ከሆነ: ፈጥኖ ያናግራል (max 2.5x)
    - TTS አጭር ከሆነ: ቀስ ያናግራል ወይም ዝምታ ይጨምራል
    """
    tts_duration = get_audio_duration(tts_path)
    if tts_duration <= 0 or target_duration <= 0:
        subprocess.run(["cp", str(tts_path), str(out_path)], check=True)
        return

    ratio = tts_duration / target_duration

    # ከ2.5x በላይ ፈጥኖ ማናገር አይቻልም — ዝምታ እናጥቃዋለን
    if ratio > 2.5:
        ratio = 2.5

    if abs(ratio - 1.0) < 0.05:
        # ልዩነቱ ትንሽ ነው — ቀጥታ ይጠቀማል
        subprocess.run(["cp", str(tts_path), str(out_path)], check=True)
        return

    atempo = build_atempo_filter(ratio)

    # ፍጥነት ቀይሮ ያቀናብራል ከዚያ ዝምታ ቢጠፋ ይጨምራል
    subprocess.run([
        "ffmpeg", "-i", str(tts_path),
        "-filter_complex",
        f"[0:a]{atempo},apad=whole_dur={target_duration}[a]",
        "-map", "[a]",
        "-t", str(target_duration),
        str(out_path), "-y"
    ], check=True, capture_output=True)


@app.post("/api/translate-video")
async def translate_video(
    video: UploadFile = File(...),
    target_language: str = Form(...),
):
    if not video.content_type or not video.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="ቪዲዮ ፋይል ብቻ ይጫኑ")

    session_id = str(uuid.uuid4())
    suffix = Path(video.filename).suffix or ".mp4"
    video_path = UPLOAD_DIR / f"{session_id}_input{suffix}"
    audio_path = UPLOAD_DIR / f"{session_id}_audio.wav"
    output_path = OUTPUT_DIR / f"{session_id}_output.mp4"
    seg_dir = UPLOAD_DIR / session_id
    seg_dir.mkdir(exist_ok=True)

    try:
        # ቪዲዮ ቀምጥ
        async with aiofiles.open(video_path, "wb") as f:
            content = await video.read()
            await f.write(content)

        # የቪዲዮ ርዝምና
        video_duration = get_audio_duration(video_path)

        # ድምፅ ወጣ
        subprocess.run([
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            str(audio_path), "-y"
        ], check=True, capture_output=True)

        # Whisper — segment timestamps ጋር ትርጉም ወጣ
        model = get_whisper_model()
        result = model.transcribe(str(audio_path), word_timestamps=False)
        segments = result.get("segments", [])
        detected_lang = result.get("language", "en")

        if not segments:
            raise HTTPException(status_code=400, detail="ድምፅ ሊሰማ አልቻለም")

        # እያንዳንዱ segment ይተረጉምና TTS ይፈጥራል
        original_texts = []
        translated_texts = []
        adjusted_segs = []  # (start, adjusted_wav_path)

        gtts_lang = target_language.split("-")[0]

        for i, seg in enumerate(segments):
            seg_start = seg["start"]
            seg_end = seg["end"]
            seg_duration = seg_end - seg_start
            seg_text = seg["text"].strip()

            if not seg_text:
                continue

            original_texts.append(seg_text)

            # ትርጉም
            try:
                translated = GoogleTranslator(
                    source=detected_lang, target=target_language
                ).translate(seg_text)
            except Exception:
                try:
                    translated = GoogleTranslator(
                        source="auto", target=target_language
                    ).translate(seg_text)
                except Exception:
                    translated = seg_text

            translated_texts.append(translated)

            # TTS ድምፅ
            tts_path = seg_dir / f"tts_{i}.mp3"
            adjusted_path = seg_dir / f"adj_{i}.wav"

            try:
                tts = gTTS(text=translated, lang=gtts_lang, slow=False)
                tts.save(str(tts_path))
                adjust_tts_speed(tts_path, seg_duration, adjusted_path)
                adjusted_segs.append((seg_start, adjusted_path))
            except Exception as e:
                print(f"Segment {i} TTS ስህተት: {e}")
                continue

        if not adjusted_segs:
            raise HTTPException(status_code=500, detail="ድምፅ መፍጠር አልተቻለም")

        # ሁሉም segments አንድ audio track ውስጥ ያስቀምጣቸዋል
        # መጀመሪያ silent base track ፍጠር
        silent_path = seg_dir / "silent_base.wav"
        subprocess.run([
            "ffmpeg", "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=stereo:d={video_duration}",
            str(silent_path), "-y"
        ], check=True, capture_output=True)

        # ffmpeg filter_complex ለ overlay ድምፆች
        inputs = ["-i", str(silent_path)]
        filter_parts = []
        mix_labels = ["[0:a]"]

        for idx, (start_sec, adj_path) in enumerate(adjusted_segs):
            inputs += ["-i", str(adj_path)]
            delay_ms = int(start_sec * 1000)
            label = f"[a{idx+1}]"
            filter_parts.append(
                f"[{idx+1}:a]adelay={delay_ms}|{delay_ms},apad=whole_dur={video_duration}{label}"
            )
            mix_labels.append(label)

        n_inputs = len(mix_labels)
        mix_label = "[mixed]"
        filter_parts.append(
            "".join(mix_labels) + f"amix=inputs={n_inputs}:duration=first:normalize=0{mix_label}"
        )

        filter_complex = ";".join(filter_parts)

        # mixed audio ፋይል
        mixed_audio_path = seg_dir / "mixed_audio.wav"
        subprocess.run(
            ["ffmpeg"] + inputs + [
                "-filter_complex", filter_complex,
                "-map", mix_label,
                "-t", str(video_duration),
                str(mixed_audio_path), "-y"
            ],
            check=True, capture_output=True
        )

        # ቪዲዮ ከ mixed audio ጋር ያዋሃዳቸዋል (ዋናው ድምፅ ይጠፋል)
        subprocess.run([
            "ffmpeg",
            "-i", str(video_path),
            "-i", str(mixed_audio_path),
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(video_duration),
            str(output_path), "-y"
        ], check=True, capture_output=True)

        # ጊዜያዊ ፋይሎችን ደምስስ
        import shutil
        shutil.rmtree(str(seg_dir), ignore_errors=True)
        for f in [video_path, audio_path]:
            if f.exists():
                f.unlink()

        return JSONResponse({
            "success": True,
            "session_id": session_id,
            "original_text": " | ".join(original_texts[:5]) + ("..." if len(original_texts) > 5 else ""),
            "translated_text": " | ".join(translated_texts[:5]) + ("..." if len(translated_texts) > 5 else ""),
            "detected_language": detected_lang,
            "segment_count": len(adjusted_segs),
            "download_url": f"/api/download/{session_id}"
        })

    except subprocess.CalledProcessError as e:
        import shutil
        shutil.rmtree(str(seg_dir), ignore_errors=True)
        for f in [video_path, audio_path]:
            if f.exists():
                f.unlink()
        raise HTTPException(status_code=500, detail=f"ቪዲዮ ማቀናበሪያ ስህተት: {e.stderr.decode()[:500]}")
    except Exception as e:
        import shutil
        shutil.rmtree(str(seg_dir), ignore_errors=True)
        for f in [video_path, audio_path]:
            if f.exists():
                f.unlink()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/{session_id}")
async def download_video(session_id: str):
    output_path = OUTPUT_DIR / f"{session_id}_output.mp4"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="ፋይሉ አልተገኘም")
    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename="addisflix_dubbed.mp4"
    )

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
