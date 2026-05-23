import os
import uuid
import subprocess
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import aiofiles
import whisper
from deep_translator import GoogleTranslator
from gtts import gTTS

app = FastAPI(title="AddisFlix Video Translator")

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
    tts_path    = UPLOAD_DIR / f"{session_id}_tts.mp3"
    output_path = OUTPUT_DIR / f"{session_id}_output.mp4"

    try:
        async with aiofiles.open(video_path, "wb") as f:
            content = await video.read()
            await f.write(content)

        subprocess.run([
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            str(audio_path), "-y"
        ], check=True, capture_output=True)

        model = get_whisper_model()
        result = model.transcribe(str(audio_path))
        original_text = result["text"].strip()
        detected_lang = result.get("language", "en")

        if not original_text:
            raise HTTPException(status_code=400, detail="ድምፅ ሊሰማ አልቻለም")

        try:
            translated_text = GoogleTranslator(
                source=detected_lang,
                target=target_language
            ).translate(original_text)
        except Exception:
            translated_text = GoogleTranslator(
                source="auto",
                target=target_language
            ).translate(original_text)

        gtts_lang = target_language.split("-")[0]
        tts = gTTS(text=translated_text, lang=gtts_lang, slow=False)
        tts.save(str(tts_path))

        subprocess.run([
            "ffmpeg", "-i", str(video_path),
            "-i", str(tts_path),
            "-c:v", "copy",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest", str(output_path), "-y"
        ], check=True, capture_output=True)

        for f in [video_path, audio_path, tts_path]:
            if f.exists():
                f.unlink()

        return JSONResponse({
            "success": True,
            "session_id": session_id,
            "original_text": original_text,
            "translated_text": translated_text,
            "detected_language": detected_lang,
            "download_url": f"/api/download/{session_id}"
        })

    except subprocess.CalledProcessError as e:
        for f in [video_path, audio_path, tts_path]:
            if f.exists():
                f.unlink()
        raise HTTPException(status_code=500, detail=f"ቪዲዮ ማቀናበሪያ ስህተት: {e.stderr.decode()}")
    except Exception as e:
        for f in [video_path, audio_path, tts_path]:
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
        filename="addisflix_translated.mp4"
    )

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
