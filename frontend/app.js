"use strict";

const WS_URL        = window.APP_CONFIG.WEBSOCKET_URL;
const REST_URL      = window.APP_CONFIG.REST_API_URL;
const TARGET_SR     = 16000;
const BATCH_SEC     = 0.5;   // 0.5s * 16000Hz * 2 bytes * 4/3 base64 = ~21KB — under API GW 32KB limit
const BATCH_SAMPLES = TARGET_SR * BATCH_SEC;
const EMA_ALPHA     = 0.35;  // weight given to each new sentiment segment; higher = more reactive
const SPARKLINE_MAX_POINTS = 120; // cap history so canvas drawing stays cheap on long sessions

/* ─── Helpers ─── */
function esc(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}
function sentColor(l) {
  return { POSITIVE:"#5fd98a", NEGATIVE:"#ff6a6a", NEUTRAL:"#d9c45f", MIXED:"#c084fc" }[l] || "#8b9097";
}
function fmtDuration(totalSec) {
  const m = Math.floor(totalSec / 60);
  const s = Math.floor(totalSec % 60);
  return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
function wordCount(str) {
  const t = (str||"").trim();
  return t ? t.split(/\s+/).length : 0;
}

/* ─── Tabs ─── */
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "live") { resizeCanvas(); resizeSparkline(); }
    if (btn.dataset.tab === "logs") loadLogs();
  });
});

/* ═══════════════════════════════════
   LIVE MIC
═══════════════════════════════════ */
const micBtn        = document.getElementById("mic-btn");
const micState       = document.getElementById("mic-state");
const micSub         = document.getElementById("mic-sub");
const micTimer        = document.getElementById("mic-timer");
const canvas         = document.getElementById("waveform");
const wCtx           = canvas.getContext("2d");
const partialEl      = document.getElementById("partial-hint");
const liveFeed       = document.getElementById("live-feed");
const liveEmpty      = document.getElementById("live-empty");
const clearBtn       = document.getElementById("clear-btn");
const copyBtn        = document.getElementById("copy-btn");
const feedStats      = document.getElementById("feed-stats");
const gaugeMarker    = document.getElementById("gauge-marker");
const gaugeCurrent   = document.getElementById("gauge-current");
const gaugeReadout   = document.getElementById("gauge-readout");
const sparkline      = document.getElementById("sparkline");
const sparkCtx       = sparkline.getContext("2d");

function resizeCanvas() {
  const r = canvas.getBoundingClientRect();
  if (r.width > 0) { canvas.width = r.width; canvas.height = r.height || 56; }
}
function resizeSparkline() {
  const r = sparkline.getBoundingClientRect();
  if (r.width > 0) { sparkline.width = r.width; sparkline.height = r.height || 40; }
  drawSparkline();
}
window.addEventListener("resize", () => { resizeCanvas(); resizeSparkline(); });

let audioCtx=null, micStream=null, sourceNode=null, analyser=null,
    processor=null, ws=null, recording=false, rafId=null;
let pcmBuf=[], pcmSamples=0;

// Running transcript + sentiment state
let fullTranscriptText = "";
let segmentCount = 0;
let sentimentEMA = 0;      // -100 (negative) .. +100 (positive)
let sparklineHistory = [];  // array of sentimentEMA snapshots over the session

// Recording duration timer
let recordStartTime = null;
let timerIntervalId = null;

// Mic volume glow (from waveform amplitude, already computed in audioStats)
let currentVolumeLevel = 0; // 0..1, smoothed

function setStatus(s, sub) { micState.textContent=s; micSub.textContent=sub; }

/* ── recording duration timer ── */
function startTimer() {
  recordStartTime = Date.now();

  if (!micTimer) {
    console.warn("[timer] #mic-timer element not found");
    return;
  }

  micTimer.textContent = "00:00";
  micTimer.style.display = "inline";

  timerIntervalId = setInterval(() => {
    const elapsed = (Date.now() - recordStartTime) / 1000;
    micTimer.textContent = fmtDuration(elapsed);
  }, 500);
}

function stopTimer() {
  if (timerIntervalId) {
    clearInterval(timerIntervalId);
    timerIntervalId = null;
  }

  if (micTimer) {
    micTimer.style.display = "none";
    micTimer.textContent = "00:00";
  }

  recordStartTime = null;
}

/* ── downsample ── */
function downsample(buf, srcSR) {
  if (srcSR === TARGET_SR) return new Float32Array(buf);
  const ratio = srcSR / TARGET_SR;
  const len   = Math.round(buf.length / ratio);
  const out   = new Float32Array(len);
  let oIdx=0, iCursor=0;
  while (oIdx < len) {
    const next = Math.round((oIdx+1)*ratio);
    let sum=0, n=0;
    for (let i=iCursor; i<next && i<buf.length; i++) { sum+=buf[i]; n++; }
    out[oIdx++] = n ? sum/n : 0;
    iCursor = next;
  }
  return out;
}

/* ── encode ── */
function toB64(f32) {
  const i16 = new Int16Array(f32.length);
  for (let i=0; i<f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    i16[i]  = s < 0 ? s*0x8000 : s*0x7FFF;
  }
  const bytes = new Uint8Array(i16.buffer);
  let bin="";
  for (let i=0; i<bytes.length; i+=0x8000)
    bin += String.fromCharCode(...bytes.subarray(i, i+0x8000));
  return btoa(bin);
}

/* ── debug: check if audio contains real signal ── */
function audioStats(f32) {
  let max=0, sum=0;
  for (let i=0; i<f32.length; i++) { const a=Math.abs(f32[i]); if(a>max) max=a; sum+=a; }
  return { max: max.toFixed(4), mean: (sum/f32.length).toFixed(6) };
}

/* ── buffer + send ── */
function enqueue(f32) {
  pcmBuf.push(f32);
  pcmSamples += f32.length;
  while (pcmSamples >= BATCH_SAMPLES) flush(false);
}

function flush(all) {
  const take = all ? pcmSamples : BATCH_SAMPLES;
  if (take === 0) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("[mic] WS not open, dropping batch");
    return;
  }
  const merged = new Float32Array(take);
  let off=0, rem=take;
  while (rem>0 && pcmBuf.length) {
    const c=pcmBuf[0], n=Math.min(c.length,rem);
    merged.set(c.subarray(0,n), off); off+=n; rem-=n;
    if (n===c.length) pcmBuf.shift(); else pcmBuf[0]=c.subarray(n);
  }
  pcmSamples -= take;

  const stats = audioStats(merged);
  console.log(`[mic] batch ${(take/TARGET_SR).toFixed(1)}s | max=${stats.max} mean=${stats.mean}`);

  if (stats.max === "0.0000") {
    console.warn("[mic] ⚠ batch is pure silence — processor may not be receiving real audio");
  }

  const b64 = toB64(merged);
  console.log(`[ws] sending audio_chunk: ${b64.length} chars`);
  ws.send(JSON.stringify({ action: "audio_chunk", audio: b64 }));
}

/* ── waveform + volume-reactive glow ── */
function drawWave() {
  if (!analyser || !recording) return;
  const data = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(data);
  const w=canvas.width, h=canvas.height;
  wCtx.clearRect(0,0,w,h);
  wCtx.beginPath();

  let peak = 0;
  for (let i=0; i<data.length; i++) {
    const y=(data[i]/128)*(h/2);
    const dev = Math.abs(data[i]-128)/128;
    if (dev > peak) peak = dev;
    i===0 ? wCtx.moveTo(0,y) : wCtx.lineTo(i*(w/data.length),y);
  }
  wCtx.strokeStyle="#ff6a3d"; wCtx.lineWidth=1.5; wCtx.stroke();

  // Smooth the peak so the glow doesn't flicker frame-to-frame
  currentVolumeLevel = currentVolumeLevel * 0.7 + peak * 0.3;
  const glowStrength = Math.min(1, currentVolumeLevel * 3.5); // amplify — raw peak is usually small
  const glowPx  = 6 + glowStrength * 22;
  const glowOpacity = 0.25 + glowStrength * 0.55;
  micBtn.style.boxShadow = `0 0 ${glowPx}px rgba(255,106,61,${glowOpacity.toFixed(2)})`;

  rafId = requestAnimationFrame(drawWave);
}

/* ── sentiment gauge ── */
// Maps a label+score into a signed value on a -100..+100 scale.
// MIXED nudges gently rather than swinging hard either direction, since its
// direction isn't well-defined by a single scalar score.
function labelToSigned(label, score) {
  const s = Math.max(0, Math.min(1, score || 0));
  switch (label) {
    case "POSITIVE": return  s * 100;
    case "NEGATIVE": return -s * 100;
    case "MIXED":    return  0; // ambiguous direction; let EMA settle toward neutral
    default:         return  0; // NEUTRAL
  }
}

function gaugeColorFor(v) {
  if (v > 15)  return "var(--positive)";
  if (v < -15) return "var(--negative)";
  return "var(--neutral)";
}

// Weighted (exponential moving average) update — each new segment nudges the
// gauge rather than snapping it, so a single noisy chunk doesn't whipsaw it.
// Alpha is scaled down for very short segments (e.g. "yeah", "um") so brief
// filler doesn't swing the average as hard as a full sentence would.
function updateGauge(label, score, segmentWordCount) {
  const signed = labelToSigned(label, score);
  const lengthFactor = Math.min(1, (segmentWordCount || 1) / 6); // ramps to full weight by ~6 words
  const alpha = EMA_ALPHA * (0.4 + 0.6 * lengthFactor);
  sentimentEMA = alpha * signed + (1 - alpha) * sentimentEMA;

  const pct = 50 + (sentimentEMA / 100) * 50; // -100..100 -> 0..100%
  const clamped = Math.max(2, Math.min(98, pct));
  gaugeMarker.style.left       = `${clamped}%`;
  gaugeMarker.style.background = gaugeColorFor(sentimentEMA);
  gaugeReadout.style.left      = `${clamped}%`;
  gaugeCurrent.textContent     = label;
  gaugeCurrent.style.color     = sentColor(label);

  const rounded = Math.round(sentimentEMA);
  gaugeReadout.textContent = rounded > 0 ? `+${rounded}` : `${rounded}`;
  gaugeReadout.style.color = gaugeColorFor(sentimentEMA);

  sparklineHistory.push(sentimentEMA);
  if (sparklineHistory.length > SPARKLINE_MAX_POINTS) sparklineHistory.shift();
  drawSparkline();
}

function resetGauge() {
  sentimentEMA = 0;
  gaugeMarker.style.left       = "50%";
  gaugeReadout.style.left = "50%";
  gaugeMarker.style.background = "var(--text)";
  gaugeCurrent.textContent     = "NEUTRAL";
  gaugeCurrent.style.color     = "";
  gaugeReadout.textContent     = "0";
  gaugeReadout.style.color     = "";
  sparklineHistory = [];
  drawSparkline();
}

/* ── sparkline (sentiment trend over the session) ── */
function drawSparkline() {
  const w = sparkline.width, h = sparkline.height;
  sparkCtx.clearRect(0,0,w,h);

  // center gridline at neutral
  sparkCtx.strokeStyle = "rgba(139,144,151,.25)";
  sparkCtx.lineWidth = 1;
  sparkCtx.beginPath();
  sparkCtx.moveTo(0, h/2);
  sparkCtx.lineTo(w, h/2);
  sparkCtx.stroke();

  if (sparklineHistory.length < 2) return;

  const n = sparklineHistory.length;
  const styles = getComputedStyle(document.documentElement);
  const colorVarFor = v => {
    if (v > 15)  return styles.getPropertyValue("--positive").trim() || "#5fd98a";
    if (v < -15) return styles.getPropertyValue("--negative").trim() || "#ff6a6a";
    return styles.getPropertyValue("--neutral").trim() || "#d9c45f";
  };

  sparkCtx.beginPath();
  sparklineHistory.forEach((v, i) => {
    const x = (i/(n-1)) * w;
    const y = h/2 - (v/100) * (h/2 - 4); // leave 4px margin top/bottom
    i === 0 ? sparkCtx.moveTo(x,y) : sparkCtx.lineTo(x,y);
  });
  const last = sparklineHistory[n-1];
  sparkCtx.strokeStyle = colorVarFor(last);
  sparkCtx.lineWidth = 2;
  sparkCtx.stroke();

  // dot at the latest point
  const lastX = w, lastY = h/2 - (last/100) * (h/2 - 4);
  sparkCtx.beginPath();
  sparkCtx.arc(lastX-2, lastY, 3, 0, Math.PI*2);
  sparkCtx.fillStyle = sparkCtx.strokeStyle;
  sparkCtx.fill();
}

/* ── feed footer stats ── */
function updateFeedStats() {
  const words = wordCount(fullTranscriptText);
  feedStats.textContent = `${words} word${words===1?"":"s"} · ${segmentCount} segment${segmentCount===1?"":"s"}`;
}

/* ── WS messages ── */
function onMsg(e) {
  let msg;
  try { msg = JSON.parse(e.data); } catch { return; }
  console.log("[ws] ←", msg);

  if (msg.type === "partial_transcript") {
    partialEl.textContent = msg.text || "";
  }
  else if (msg.type === "final_transcript") {
    partialEl.textContent = "";
    // NOTE: msg.text is treated as the NEW segment only (not the cumulative
    // transcript). If the backend instead sends a cumulative full_transcript
    // field with no separate delta, this needs to diff against
    // fullTranscriptText and append only the new suffix instead.
    appendTranscriptSegment(msg.text,
                             msg.sentiment_label || "NEUTRAL",
                             msg.sentiment_score || 0);
  }
  else if (msg.type === "error") {
    micSub.textContent = `⚠ ${msg.message}`;
    console.error("[ws] server error:", msg.message);
  }
}

/* Continuous Transcript (Single Growing Paragraph, newest words fade in) */
function appendTranscriptSegment(newWords, label, score) {
  if (!newWords || !newWords.trim()) return;

  const trimmed = newWords.trim();
  liveEmpty.style.display = "none";
  clearBtn.style.display  = "inline-block";
  copyBtn.style.display   = "inline-block";
  feedStats.style.display = "flex"

  const hadText = fullTranscriptText.length > 0;
  fullTranscriptText += (hadText ? " " : "") + trimmed;
  segmentCount++;

  let transcriptDiv = document.getElementById("continuous-transcript");
  if (!transcriptDiv) {
    transcriptDiv = document.createElement("div");
    transcriptDiv.id = "continuous-transcript";
    transcriptDiv.className = "transcript-chunk";
    transcriptDiv.innerHTML = `<div class="transcript-text" style="white-space: pre-wrap; line-height: 1.6; font-size: 1.05em;"></div>`;
    liveFeed.prepend(transcriptDiv);
  }
  transcriptDiv.style.borderLeftColor = sentColor(label);

  const textEl = transcriptDiv.querySelector(".transcript-text");
  if (hadText) textEl.appendChild(document.createTextNode(" "));
  const newSpan = document.createElement("span");
  newSpan.className = "word-fade-in";
  newSpan.textContent = trimmed;
  textEl.appendChild(newSpan);

  // Auto-scroll the feed to follow the growing paragraph
  liveFeed.scrollTop = liveFeed.scrollHeight;

  updateGauge(label, score, wordCount(trimmed));
  updateFeedStats();
}

/* ── copy transcript ── */
copyBtn.addEventListener("click", async () => {
  if (!fullTranscriptText) return;
  try {
    await navigator.clipboard.writeText(fullTranscriptText);
    const original = copyBtn.textContent;
    copyBtn.textContent = "Copied ✓";
    setTimeout(() => { copyBtn.textContent = original; }, 1500);
  } catch (err) {
    console.error("[clipboard] failed:", err);
    copyBtn.textContent = "Copy failed";
    setTimeout(() => { copyBtn.textContent = "Copy transcript"; }, 1500);
  }
});

/* ── START ── */
async function startRecording() {
  setStatus("Connecting…", "Requesting mic permission…");

  // 1. Get microphone
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch(err) {
    setStatus("Idle", `Mic error: ${err.message}`);
    console.error("[mic] getUserMedia failed:", err);
    return;
  }
  const track = micStream.getAudioTracks()[0];
  console.log("[mic] granted:", track.label, track.getSettings());

  // 2. Create AudioContext and EXPLICITLY resume it
  // Chrome suspends AudioContext until a user gesture — we are inside one (click),
  // but must still call resume() explicitly to guarantee running state.
  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_SR });
  if (audioCtx.state === "suspended") {
    await audioCtx.resume();
    console.log("[audio] context resumed");
  }
  console.log("[audio] state:", audioCtx.state, "sampleRate:", audioCtx.sampleRate);

  // 3. Wire nodes
  sourceNode = audioCtx.createMediaStreamSource(micStream);

  analyser       = audioCtx.createAnalyser();
  analyser.fftSize = 1024;

  // ScriptProcessorNode — captures raw PCM from the mic
  // bufferSize 2048: smaller = more frequent callbacks = less delay before first batch
  processor = audioCtx.createScriptProcessor(2048, 1, 1);

  // Connection order matters:
  // source → analyser (for waveform display, no audio output needed)
  // source → processor → destination (processor MUST reach destination to stay alive in Chrome)
  sourceNode.connect(analyser);
  sourceNode.connect(processor);
  processor.connect(audioCtx.destination);

  // 4. Capture samples
  processor.onaudioprocess = ev => {
    if (!recording) return;
    // getChannelData(0) = left channel / mono
    const raw = ev.inputBuffer.getChannelData(0);

    // Log the first few callbacks to confirm real audio is arriving
    if (pcmSamples < TARGET_SR) {
      const s = audioStats(raw);
      console.log(`[proc] frame ${ev.inputBuffer.length}smp | max=${s.max} mean=${s.mean} | ctx=${audioCtx.state}`);
    }

    // Normalize captured audio to the 16 kHz rate expected by the backend.
    const normalized = downsample(raw, ev.inputBuffer.sampleRate);
    enqueue(normalized);
  };

  resizeCanvas();
  resizeSparkline();

  // 5. Open WebSocket
  setStatus("Connecting…", "Opening WebSocket…");
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[ws] open");
    recording = true;
    micBtn.classList.add("recording");
    micBtn.setAttribute("aria-label", "Stop recording");
    setStatus("Recording", "Streaming audio chunks - speak clearly");
    startTimer();
    rafId = requestAnimationFrame(drawWave);
  };

  ws.onmessage = onMsg;

  ws.onerror = ev => {
    console.error("[ws] error:", ev);
    setStatus("Error", "WebSocket error — open DevTools console for details");
  };

  ws.onclose = ev => {
    console.log(`[ws] closed code=${ev.code} reason="${ev.reason}" clean=${ev.wasClean}`);
    if (recording) {
      setStatus("Idle", `Connection dropped (code ${ev.code}) — see console`);
      cleanupAudio();
      recording = false;
      micBtn.classList.remove("recording");
      micBtn.style.boxShadow = "";
      stopTimer();
    }
  };
}

/* ── STOP ── */
function stopRecording() {
  recording = false;
  micBtn.classList.remove("recording");
  micBtn.setAttribute("aria-label", "Start recording");
  micBtn.style.boxShadow = "";
  setStatus("Idle", "Click to start streaming");
  partialEl.textContent = "";
  flush(true);
  cleanupAudio();
  stopTimer();
  setTimeout(() => { if(ws){ ws.close(1000,"user stopped"); ws=null; } }, 1500);
}

function cleanupAudio() {
  if (rafId)     { cancelAnimationFrame(rafId); rafId=null; }
  if (processor) { processor.onaudioprocess=null; processor.disconnect(); processor=null; }
  if (sourceNode){ sourceNode.disconnect(); sourceNode=null; }
  analyser = null;
  wCtx.clearRect(0,0,canvas.width,canvas.height);
  if (micStream) { micStream.getTracks().forEach(t=>t.stop()); micStream=null; }
  if (audioCtx)  { audioCtx.close(); audioCtx=null; }
  pcmBuf=[]; pcmSamples=0;
  currentVolumeLevel = 0;
}

micBtn.addEventListener("click", () => recording ? stopRecording() : startRecording());
clearBtn.addEventListener("click", () => {
  liveFeed.innerHTML="";
  liveFeed.appendChild(liveEmpty);
  liveEmpty.style.display="";
  clearBtn.style.display="none";
  copyBtn.style.display="none";
  feedStats.style.display="none"
  fullTranscriptText = "";
  segmentCount = 0;
  resetGauge();
  updateFeedStats();
});

/* ── keyboard shortcut: Space or R toggles recording ──
   Ignored while focus is in an input/textarea/contentEditable so it doesn't
   hijack normal typing elsewhere on the page (e.g. future form fields). */
window.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  const isTyping = tag === "input" || tag === "textarea" || e.target.isContentEditable;
  if (isTyping) return;
  // Only act while the Live Mic tab is active
  const liveTabActive = document.getElementById("tab-live").classList.contains("active");
  if (!liveTabActive) return;

  if (e.code === "Space" || e.code === "KeyR") {
    e.preventDefault();
    recording ? stopRecording() : startRecording();
  }
});


/* ═══════════════════════════════════
   FILE UPLOAD
═══════════════════════════════════ */
const dropzone     = document.getElementById("dropzone");
const fileInput    = document.getElementById("file-input");
const fileRow      = document.getElementById("file-row");
const fileLabel    = document.getElementById("file-label");
const uploadBtn    = document.getElementById("upload-btn");
const progressBar  = document.getElementById("progress-bar");
const progressFill = document.getElementById("progress-fill");
const uploadMsg    = document.getElementById("upload-msg");
const resultCard   = document.getElementById("result-card");
const resultText   = document.getElementById("result-transcript");
const resultDot    = document.getElementById("result-dot");
const resultLbl    = document.getElementById("result-label");
const resultMeta   = document.getElementById("result-meta");

let selectedFile = null;

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover",  e => { e.preventDefault(); dropzone.classList.add("drag-over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag-over"));
dropzone.addEventListener("drop", e => {
  e.preventDefault(); dropzone.classList.remove("drag-over");
  if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => { if (fileInput.files[0]) selectFile(fileInput.files[0]); });

function selectFile(f) {
  selectedFile = f;
  fileLabel.textContent     = `${f.name}  (${(f.size/1024/1024).toFixed(2)} MB)`;
  fileRow.style.display     = "flex";
  resultCard.style.display  = "none";
  uploadMsg.textContent     = "";
  progressFill.style.width  = "0%";
  progressBar.style.display = "none";
}

function setProgress(pct, msg) {
  progressBar.style.display = "block";
  progressFill.style.width  = `${pct}%`;
  uploadMsg.textContent     = msg;
}

uploadBtn.addEventListener("click", async () => {
  if (!selectedFile) return;
  uploadBtn.disabled = true;
  resultCard.style.display = "none";
  try {
    setProgress(8, "Requesting upload URL…");
    const ext    = selectedFile.name.split(".").pop().toLowerCase();
    const urlRes = await fetch(`${REST_URL}/upload-url`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ file_extension: ext }),
    });
    if (!urlRes.ok) throw new Error(`Upload URL failed (${urlRes.status})`);
    const { job_id, upload_url } = await urlRes.json();

    setProgress(25, "Uploading to S3…");
    const putRes = await fetch(upload_url, {
      method:"PUT", headers:{"Content-Type":"application/octet-stream"}, body: selectedFile,
    });
    if (!putRes.ok) throw new Error(`S3 upload failed (${putRes.status})`);

    setProgress(45, "Transcribing…");
    const entry = await poll(job_id, p => setProgress(45+p*50, "Transcribing…"));
    setProgress(100, "Done ✓");
    showResult(entry);
  } catch(err) {
    uploadMsg.textContent    = `⚠ ${err.message}`;
    progressFill.style.width = "0%";
  } finally {
    uploadBtn.disabled = false;
  }
});

async function poll(jobId, onProg, attempt=0) {
  const MAX=72;
  if (attempt>=MAX) throw new Error("Timed out — check Log History tab shortly");
  const res = await fetch(`${REST_URL}/logs?limit=100`);
  if (res.ok) {
    const data  = await res.json();
    const match = (data.logs||[]).find(l=>l.job_id===jobId);
    if (match) {
      if (match.status === "FAILED") {
        throw new Error(match.failure_reason || "AWS Transcribe failed");
      }

      return match;
    }
  }
  onProg(Math.min(attempt/MAX, 0.95));
  await new Promise(r=>setTimeout(r, 5000));
  return poll(jobId, onProg, attempt+1);
}

function showResult(e) {
  const label = e.sentiment_label||"NEUTRAL";
  const score = typeof e.sentiment_score==="number" ? e.sentiment_score : 0;
  const dur   = e.duration_seconds ? `${parseFloat(e.duration_seconds).toFixed(1)}s` : "";
  resultCard.className     = `result-card ${label}`;
  resultText.textContent   = e.transcript||"(no speech detected)";
  resultDot.className      = `sent-dot ${label}`;
  resultLbl.textContent    = `${label} · ${(score*100).toFixed(0)}%`;
  resultMeta.textContent   = [dur,`engine: ${e.sentiment_engine||"—"}`,`job: ${e.job_id||"—"}`].filter(Boolean).join("  ·  ");
  resultCard.style.display = "block";
}


/* ═══════════════════════════════════
   LOGS
═══════════════════════════════════ */
const logBody    = document.getElementById("log-body");
const logCount   = document.getElementById("log-count");
const refreshBtn = document.getElementById("refresh-btn");
refreshBtn.addEventListener("click", loadLogs);

async function loadLogs() {
  logBody.innerHTML    = `<tr><td colspan="5" class="empty-hint">Loading…</td></tr>`;
  logCount.textContent = "…";
  try {
    const res  = await fetch(`${REST_URL}/logs?limit=100`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderLogs(data.logs||[]);
  } catch(err) {
    logBody.innerHTML    = `<tr><td colspan="5" class="empty-hint" style="color:var(--negative)">Failed: ${esc(err.message)}</td></tr>`;
    logCount.textContent = "error";
  }
}

function renderLogs(logs) {
  logCount.textContent = `${logs.length} entries`;
  if (!logs.length) { logBody.innerHTML=`<tr><td colspan="5" class="empty-hint">No entries yet.</td></tr>`; return; }
  logBody.innerHTML = logs.map(e => {
    const time    = e.timestamp_utc ? new Date(e.timestamp_utc).toLocaleString() : "—";
    const src     = e.source||"—";
    const label   = e.sentiment_label||"NEUTRAL";
    const score   = typeof e.sentiment_score==="number" ? e.sentiment_score : 0;
    const snippet = (e.transcript||"").slice(0,90)+((e.transcript||"").length>90?"…":"");
    const dim     = e.session_summary ? "opacity:.65;" : "";
    return `<tr style="${dim}">
      <td class="col-time">${esc(time)}</td>
      <td><span class="source-badge source-${src}">${src}${e.session_summary?" ∑":""}</span></td>
      <td class="col-text" title="${esc(e.transcript||"")}">${esc(snippet)||'<em style="color:var(--text-dim)">empty</em>'}</td>
      <td><span style="color:${sentColor(label)}">${label}</span></td>
      <td style="font-family:var(--mono);font-size:12px;">${(score*100).toFixed(0)}%</td>
    </tr>`;
  }).join("");
}

loadLogs();