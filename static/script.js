const videoInput = document.getElementById('video-input');
const dropArea = document.getElementById('drop-area');
const filePreview = document.getElementById('file-preview');
const previewVideo = document.getElementById('preview-video');
const fileName = document.getElementById('file-name');
const removeFile = document.getElementById('remove-file');
const targetLang = document.getElementById('target-lang');
const translateBtn = document.getElementById('translate-btn');
const progressSection = document.getElementById('progress-section');
const resultSection = document.getElementById('result-section');
const errorSection = document.getElementById('error-section');
const progressBar = document.getElementById('progress-bar');

let selectedFile = null;

// ቋንቋዎችን ከ API ጫን
async function loadLanguages() {
  try {
    const res = await fetch('/api/languages');
    const data = await res.json();
    Object.entries(data.languages).forEach(([code, name]) => {
      const opt = document.createElement('option');
      opt.value = code;
      opt.textContent = name;
      targetLang.appendChild(opt);
    });
  } catch (e) {
    console.error('ቋንቋዎችን መጫን አልተቻለም:', e);
  }
}

// ፋይል ምረጫ
videoInput.addEventListener('change', (e) => {
  if (e.target.files[0]) handleFile(e.target.files[0]);
});

// Drag & Drop
dropArea.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropArea.classList.add('dragover');
});
dropArea.addEventListener('dragleave', () => dropArea.classList.remove('dragover'));
dropArea.addEventListener('drop', (e) => {
  e.preventDefault();
  dropArea.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file && file.type.startsWith('video/')) handleFile(file);
});

function handleFile(file) {
  selectedFile = file;
  const url = URL.createObjectURL(file);
  previewVideo.src = url;
  fileName.textContent = file.name;
  dropArea.style.display = 'none';
  filePreview.style.display = 'block';
  updateTranslateBtn();
}

removeFile.addEventListener('click', () => {
  selectedFile = null;
  previewVideo.src = '';
  videoInput.value = '';
  filePreview.style.display = 'none';
  dropArea.style.display = 'block';
  updateTranslateBtn();
});

targetLang.addEventListener('change', updateTranslateBtn);

function updateTranslateBtn() {
  translateBtn.disabled = !(selectedFile && targetLang.value);
}

// ደረጃዎች ማዘመን
function setStep(stepNum, status) {
  const step = document.getElementById(`step-${stepNum}`);
  if (!step) return;
  step.classList.remove('active', 'done');
  const statusEl = step.querySelector('.step-status');
  if (status === 'active') {
    step.classList.add('active');
    statusEl.textContent = '⏳';
  } else if (status === 'done') {
    step.classList.add('done');
    statusEl.textContent = '✅';
  } else {
    statusEl.textContent = '⏸️';
  }
}

function setProgress(percent) {
  progressBar.style.width = percent + '%';
}

// ትርጉም ይጀምር
translateBtn.addEventListener('click', async () => {
  if (!selectedFile || !targetLang.value) return;

  // ገጾችን ዳግም አስጀምር
  progressSection.style.display = 'block';
  resultSection.style.display = 'none';
  errorSection.style.display = 'none';
  translateBtn.disabled = true;

  // ሁሉም ደረጃዎች ዳግም አስጀምር
  [1, 2, 3, 4, 5].forEach(i => setStep(i, 'waiting'));
  setProgress(0);

  // ደረጃ 1 ጀምር (Demucs — ረጅም ጊዜ ይወስዳል)
  setStep(1, 'active');
  setProgress(5);

  // ቅጹ ፍጠር
  const formData = new FormData();
  formData.append('video', selectedFile);
  formData.append('target_language', targetLang.value);

  try {
    // Demucs ረጅም ጊዜ ስለሚወስድ timers ይረዝማሉ
    const t1 = setTimeout(() => {
      setStep(1, 'done'); setStep(2, 'active'); setProgress(30);
    }, 20000);
    const t2 = setTimeout(() => {
      setStep(2, 'done'); setStep(3, 'active'); setProgress(55);
    }, 35000);
    const t3 = setTimeout(() => {
      setStep(3, 'done'); setStep(4, 'active'); setProgress(70);
    }, 50000);
    const t4 = setTimeout(() => {
      setStep(4, 'done'); setStep(5, 'active'); setProgress(88);
    }, 65000);

    const res = await fetch('/api/translate-video', {
      method: 'POST',
      body: formData,
    });

    [t1, t2, t3, t4].forEach(clearTimeout);

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'ያልታወቀ ስህተት');
    }

    const data = await res.json();

    // ሁሉም ደረጃዎች ተጠናቀቁ
    setStep(1, 'done');
    setStep(2, 'done');
    setStep(3, 'done');
    setStep(4, 'done');
    setStep(5, 'done');
    setProgress(100);

    // ውጤት አሳይ
    setTimeout(() => {
      progressSection.style.display = 'none';
      resultSection.style.display = 'block';

      const segInfo = data.segment_count ? ` (${data.segment_count} አረፍተ ነገሮች)` : "";
      document.getElementById('original-text').textContent = data.original_text + segInfo;
      document.getElementById('translated-text').textContent = data.translated_text;

      const resultVideo = document.getElementById('result-video');
      resultVideo.src = data.download_url;

      const downloadBtn = document.getElementById('download-btn');
      downloadBtn.href = data.download_url;
    }, 800);

  } catch (e) {
    progressSection.style.display = 'none';
    errorSection.style.display = 'block';
    document.getElementById('error-message').textContent = e.message;
    translateBtn.disabled = false;
  }
});

// አዲስ ትርጉም
document.getElementById('new-translate-btn').addEventListener('click', resetApp);
document.getElementById('retry-btn').addEventListener('click', resetApp);

function resetApp() {
  selectedFile = null;
  previewVideo.src = '';
  videoInput.value = '';
  filePreview.style.display = 'none';
  dropArea.style.display = 'block';
  targetLang.value = '';
  resultSection.style.display = 'none';
  errorSection.style.display = 'none';
  progressSection.style.display = 'none';
  setProgress(0);
  updateTranslateBtn();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ጀምር
loadLanguages();
