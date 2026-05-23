import os
import uuid
import subprocess
import shutil
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


def get_duration(path: Path) -> float:
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def build_atempo(ratio: float) -> str:
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


def adjust_tts(tts_path: Path, target_dur: float, out_path: Path):
    """TTS ድምፅ ርዝምና ከ segment ጋር ያስተካክላል"""
    tts_dur = get_duration(tts_path)
    if tts_dur <= 0 or target_dur <= 0:
        shutil.copy(str(tts_path), str(out_path))
        return

    ratio = tts_dur / target_dur
    ratio = min(ratio, 2.5)

    if abs(ratio - 1.0) < 0.05:
        shutil.copy(str(tts_path), str(out_path))
        return

    atempo = build_atempo(ratio)
    subprocess.run([
        "ffmpeg", "-i", str(tts_path),
        "-filter_complex",
        f"[0:a]{atempo},apad=whole_dur={target_dur}[a]",
        "-map", "[a]",
        "-t", str(target_dur),
        str(out_path), "-y"
    ], check=True, capture_output=True)


def separate_vocals(audio_path: Path, work_dir: Path):
    """
    Demucs ተጠቅሞ ቃላት ድምፅ ከ Background ይለያቸዋል።
    ይመልሳል: (vocals_path, no_vocals_path)
    """
    demucs_out = work_dir / "demucs_out"
    demucs_out.mkdir(exist_ok=True)

    subprocess.run([
        "python", "-m", "demucs",
        "--two-stems=vocals",
        "--out", str(demucs_out),
        str(audio_path)
    ], check=True, capture_output=True)

    # Demucs output: demucs_out/htdemucs/<stem_name>/vocals.wav + no_vocals.wav
    stem_dirs = list(demucs_out.glob("*"))
    if not stem_dirs:
        raise RuntimeError("Demucs output አልተገኘም")

    model_dir = stem_dirs[0]
    audio_stem = audio_path.stem
    result_dir = model_dir / audio_stem

    vocals_path    = result_dir / "vocals.wav"
    no_vocals_path = result_dir / "no_vocals.wav"

    if not vocals_path.exists() or not no_vocals_path.exists():
        raise RuntimeError(f"Demucs ፋይሎች አልተገኙም: {result_dir}")

    return vocals_path, no_vocals_path


@app.post("/api/translate-video")
async def translate_video(
    video: UploadFile = File(...),
    target_language: str = Form(...),
):
    if not video.content_type or not video.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="ቪዲዮ ፋይል ብቻ ይጫኑ")

    session_id = str(uuid.uuid4())
    suffix = Path(video.filename).suffix or ".mp4"
    video_path  = UPLOAD_DIR / f"{session_id}_input{suffix}"
    audio_path  = UPLOAD_DIR / f"{session_id}_audio.wav"
    output_path = OUTPUT_DIR / f"{session_id}_output.mp4"
    work_dir    = UPLOAD_DIR / session_id
    work_dir.mkdir(exist_ok=True)

    try:
        # 1. ቪዲዮ ቀምጥ
        async with aiofiles.open(video_path, "wb") as f:
            await f.write(await video.read())

        video_duration = get_duration(video_path)

        # 2. ድምፅ ወጣ (44100 Hz stereo — Demucs ይፈልጋል)
        subprocess.run([
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "44100", "-ac", "2",
            str(audio_path), "-y"
        ], check=True, capture_output=True)

        # 3. Demucs — ቃላት vs Background ይለያቸዋል
        print("Demucs: ድምፅ እየለያ...")
        vocals_path, bg_path = separate_vocals(audio_path, work_dir)

        # 4. Whisper — vocals track ብቻ ትርጉም ወጣ (ትክክለኛ timestamps)
        vocals_16k = work_dir / "vocals_16k.wav"
        subprocess.run([
            "ffmpeg", "-i", str(vocals_path),
            "-ar", "16000", "-ac", "1",
            str(vocals_16k), "-y"
        ], check=True, capture_output=True)

        model = get_whisper_model()
        result = model.transcribe(str(vocals_16k), word_timestamps=False)
        segments = result.get("segments", [])
        detected_lang = result.get("language", "en")

        if not segments:
            raise HTTPException(status_code=400, detail="ድምፅ ሊሰማ አልቻለም")

        # 5. እያንዳንዱ segment ትርጉምና TTS
        gtts_lang = target_language.split("-")[0]
        original_texts   = []
        translated_texts = []
        tts_segments     = []  # (start_sec, adjusted_wav_path)

        for i, seg in enumerate(segments):
            seg_start = seg["start"]
            seg_end   = seg["end"]
            seg_dur   = seg_end - seg_start
            seg_text  = seg["text"].strip()
            if not seg_text:
                continue

            original_texts.append(seg_text)

            # ትርጉም
            try:
                translated = GoogleTranslator(source=detected_lang, target=target_language).translate(seg_text)
            except Exception:
                try:
                    translated = GoogleTranslator(source="auto", target=target_language).translate(seg_text)
                except Exception:
                    translated = seg_text
            translated_texts.append(translated)

            # TTS + ፍጥነት ቅናሽ/ጭማሪ
            tts_raw = work_dir / f"tts_{i}.mp3"
            tts_adj = work_dir / f"adj_{i}.wav"
            try:
                gTTS(text=translated, lang=gtts_lang, slow=False).save(str(tts_raw))
                adjust_tts(tts_raw, seg_dur, tts_adj)
                tts_segments.append((seg_start, tts_adj))
            except Exception as e:
                print(f"Segment {i} TTS ስህተት: {e}")
                continue

        if not tts_segments:
            raise HTTPException(status_code=500, detail="ድምፅ መፍጠር አልተቻለም")

        # 6. TTS segments → አንድ audio track (ዝምታ base ላይ overlay)
        silent_path = work_dir / "silent.wav"
        subprocess.run([
            "ffmpeg", "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=stereo:d={video_duration}",
            str(silent_path), "-y"
        ], check=True, capture_output=True)

        inputs       = ["-i", str(silent_path)]
        filter_parts = []
        mix_labels   = ["[0:a]"]

        for idx, (start_sec, adj_path) in enumerate(tts_segments):
            inputs += ["-i", str(adj_path)]
            delay_ms = int(start_sec * 1000)
            label = f"[a{idx+1}]"
            filter_parts.append(
                f"[{idx+1}:a]adelay={delay_ms}|{delay_ms},"
                f"apad=whole_dur={video_duration}{label}"
            )
            mix_labels.append(label)

        n = len(mix_labels)
        tts_mix_label = "[tts_mixed]"
        filter_parts.append(
            "".join(mix_labels) +
            f"amix=inputs={n}:duration=first:normalize=0{tts_mix_label}"
        )

        tts_track = work_dir / "tts_track.wav"
        subprocess.run(
            ["ffmpeg"] + inputs + [
                "-filter_complex", ";".join(filter_parts),
                "-map", tts_mix_label,
                "-t", str(video_duration),
                str(tts_track), "-y"
            ],
            check=True, capture_output=True
        )

        # 7. TTS track + Background ድምፅ ያዋሃዳቸዋል
        final_audio = work_dir / "final_audio.wav"
        subprocess.run([
            "ffmpeg",
            "-i", str(tts_track),
            "-i", str(bg_path),
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[out]",
            "-map", "[out]",
            "-t", str(video_duration),
            str(final_audio), "-y"
        ], check=True, capture_output=True)

        # 8. ቪዲዮ + final audio (ዋናው ድምፅ ሳይኖር)
        subprocess.run([
            "ffmpeg",
            "-i", str(video_path),
            "-i", str(final_audio),
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(video_duration),
            str(output_path), "-y"
        ], check=True, capture_output=True)

        # ጊዜያዊ ፋይሎች ደምስስ
        shutil.rmtree(str(work_dir), ignore_errors=True)
        for f in [video_path, audio_path]:
            if f.exists():
                f.unlink()

        return JSONResponse({
            "success": True,
            "session_id": session_id,
            "original_text": " | ".join(original_texts[:5]) + ("..." if len(original_texts) > 5 else ""),
            "translated_text": " | ".join(translated_texts[:5]) + ("..." if len(translated_texts) > 5 else ""),
            "detected_language": detected_lang,
            "segment_count": len(tts_segments),
            "download_url": f"/api/download/{session_id}"
        })

    except subprocess.CalledProcessError as e:
        shutil.rmtree(str(work_dir), ignore_errors=True)
        for f in [video_path, audio_path]:
            if f.exists():
                f.unlink()
        raise HTTPException(status_code=500, detail=f"ቪዲዮ ማቀናበሪያ ስህተት: {e.stderr.decode()[:500]}")
    except Exception as e:
        shutil.rmtree(str(work_dir), ignore_errors=True)
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
