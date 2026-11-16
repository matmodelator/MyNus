/*
 * 9.0.3 Save Video 
 */

const VIDEO_DRIFT_SECONDS = 0.2;
const VIDEO_CORRECTION_INTERVAL_MS = 500;

function readBox(view, start, end) {
  const remaining = end - start;
  if (remaining < 8 || view.byteLength < 8) return null;
  let size = view.getUint32(0);
  const type = String.fromCharCode(...new Uint8Array(view.buffer, view.byteOffset + 4, 4));
  const header = size === 1 ? 16 : 8;
  if (header === 16) {
    if (view.byteLength < 16) return null;
    size = Number(view.getBigUint64(8));
  } else if (size === 0) {
    size = remaining;
  }
  if (!Number.isSafeInteger(size) || size < header || size > remaining) return null;
  return { type, start: start + header, end: start + size };
}

function* childBoxes(view, start, end) {
  for (let pos = start; end - pos >= 8;) {
    const bytes = new DataView(view.buffer, pos, Math.min(16, end - pos));
    const box = readBox(bytes, pos, end);
    if (!box) return;
    yield box;
    pos = box.end;
  }
}

function childBox(view, parent, type) {
  if (!parent) return null;
  for (const box of childBoxes(view, parent.start, parent.end)) {
    if (box.type === type) return box;
  }
  return null;
}

async function readFrameTiming(file) {
  for (let pos = 0; file.size - pos >= 8;) {
    const header = new DataView(await file.slice(pos, pos + 16).arrayBuffer());
    const box = readBox(header, pos, file.size);
    if (!box) return null;
    pos = box.end;
    if (box.type !== "moov") continue;
    const movie = new DataView(await file.slice(box.start, box.end).arrayBuffer());
    for (const track of childBoxes(movie, 0, movie.byteLength)) {
      if (track.type !== "trak") continue;
      const media = childBox(movie, track, "mdia");
      const handler = childBox(movie, media, "hdlr");
      if (!handler || handler.end - handler.start < 12
        || movie.getUint32(handler.start + 8) !== 0x76696465) continue;
      const clock = childBox(movie, media, "mdhd");
      const samples = childBox(movie, childBox(movie, media, "minf"), "stbl");
      const timing = childBox(movie, samples, "stts");
      if (!clock || clock.end - clock.start < 16 || !timing || timing.end - timing.start < 8) continue;
      const scaleField = clock.start + (movie.getUint8(clock.start) === 1 ? 20 : 12);
      if (scaleField + 4 > clock.end) continue;
      const scale = movie.getUint32(scaleField);
      const entries = movie.getUint32(timing.start + 4);
      if (!scale || entries > Math.floor((timing.end - timing.start - 8) / 8)) continue;
      let frames = 0;
      let ticks = 0;
      let delta = 0;
      let variable = false;
      for (let entry = 0; entry < entries; entry++) {
        const at = timing.start + 8 + entry * 8;
        const count = movie.getUint32(at);
        const sampleDelta = movie.getUint32(at + 4);
        if (!count) continue;
        if (frames && sampleDelta !== delta) variable = true;
        delta = sampleDelta;
        frames += count;
        ticks += count * sampleDelta;
      }
      if (frames && ticks > 0) return { fps: frames * scale / ticks, variable };
    }
    return null;
  }
  return null;
}

/**
 * Binds an existing Karaoke video element and an optional Master Timeline overlay.
 * MyNus supplies the audio position through syncFromAudio() and pauses via pause().
 * A positive offset places the start of the video later on the audio timeline.
 */
export function createKaraokeVideo({
  video,
  overlay,
  getTimelineScale,
  onOffsetChange,
  onError = console.error,
}) {
  const listeners = new AbortController();
  let sourceUrl = null;
  let sourceFile = null;
  let frameTiming = null;
  let offset = 0;
  let loop = false;
  let active = false;
  let audioTime = 0;
  let audioPlaying = false;
  let previousTarget = null;
  let previousCycle = null;
  let lastCorrection = -Infinity;
  let playbackRevision = 0;
  let pendingPlay = null;
  let playbackError = null;
  let drag = null;

  video.preload = "metadata";
  video.controls = false;
  video.loop = false;
  video.hidden = true;

  function duration() {
    return sourceUrl && Number.isFinite(video.duration) && video.duration > 0
      ? video.duration : 0;
  }

  function renderTimelineOverlay() {
    if (!overlay) return;
    const length = duration();
    const scale = getTimelineScale?.();
    overlay.hidden = !length || !Number.isFinite(scale) || scale <= 0;
    if (overlay.hidden) return;
    overlay.style.left = `${offset * scale}px`;
    overlay.style.width = `${length * scale}px`;
    overlay.style.removeProperty("--video-frame-spacing");
    overlay.title = `Video: ${length.toFixed(2)} s · Offset: ${offset.toFixed(2)} s`;
    if (frameTiming) {
      const { fps, variable } = frameTiming;
      const pixelsPerFrame = scale / fps;
      const framesPerTick = Math.max(1, Math.ceil(7 / pixelsPerFrame));
      overlay.style.setProperty("--video-frame-spacing", `${framesPerTick * pixelsPerFrame}px`);
      overlay.title += ` · ${variable ? "≈" : ""}${Number(fps.toFixed(3))} fps · ${framesPerTick} frame(s) per mark`;
    }
  }

  function pauseVideo() {
    playbackRevision++;
    pendingPlay = null;
    if (!video.paused) video.pause();
  }

  function playVideo() {
    if (pendingPlay || playbackError || !video.paused) return;
    const revision = playbackRevision;
    pendingPlay = video.play();
    pendingPlay.catch(error => {
      if (revision !== playbackRevision) return;
      playbackError = error;
      onError(error);
    }).finally(() => {
      if (revision === playbackRevision) pendingPlay = null;
    });
  }

  function syncFromAudio(time, playing, { seek = false } = {}) {
    if (!Number.isFinite(time)) return;
    if (playing && !audioPlaying) playbackError = null;
    audioTime = time;
    audioPlaying = playing;

    const length = duration();
    const elapsed = time - offset;
    if (!active || !length || elapsed < 0 || (!loop && elapsed >= length)) {
      if (pendingPlay || !video.paused) pauseVideo();
      video.hidden = true;
      previousTarget = previousCycle = null;
      return;
    }

    const cycle = loop ? Math.floor(elapsed / length) : 0;
    const target = loop ? elapsed % length : elapsed;
    const now = performance.now();
    const jumped = seek || previousTarget === null || cycle !== previousCycle
      || target < previousTarget;
    const drifted = !video.seeking && now - lastCorrection >= VIDEO_CORRECTION_INTERVAL_MS
      && Math.abs(video.currentTime - target) > VIDEO_DRIFT_SECONDS;

    video.hidden = false;
    if (jumped || drifted) {
      if (Math.abs(video.currentTime - target) > 0.001 || video.seeking) {
        video.currentTime = target;
      }
      lastCorrection = now;
    }
    previousTarget = target;
    previousCycle = cycle;

    if (playing) playVideo();
    else if (pendingPlay || !video.paused) pauseVideo();
  }

  function pause() {
    audioPlaying = false;
    pauseVideo();
  }

  function setActive(value) {
    active = Boolean(value);
    syncFromAudio(audioTime, audioPlaying, { seek: true });
  }

  function clearVideo() {
    pauseVideo();
    video.hidden = true;
    if (sourceUrl) {
      const oldUrl = sourceUrl;
      sourceUrl = null;
      sourceFile = null;
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(oldUrl);
    }
    playbackError = null;
    frameTiming = null;
    previousTarget = previousCycle = null;
    renderTimelineOverlay();
  }

  function loadVideo(file) {
    const nextUrl = URL.createObjectURL(file);
    clearVideo();
    sourceUrl = nextUrl;
    sourceFile = file;
    video.src = nextUrl;
    readFrameTiming(file).then(timing => {
      if (sourceUrl !== nextUrl) return;
      frameTiming = timing;
      renderTimelineOverlay();
    }).catch(() => {
      // A playable file can have no readable frame table.
    });
  }

  function setVolume(value) {
    if (Number.isFinite(value)) video.volume = Math.min(1, Math.max(0, value));
  }

  function setOffset(seconds) {
    if (!Number.isFinite(seconds) || offset === seconds) return;
    offset = seconds;
    renderTimelineOverlay();
    syncFromAudio(audioTime, audioPlaying, { seek: true });
    onOffsetChange?.(offset);
  }

  function setLoop(value) {
    loop = Boolean(value);
    syncFromAudio(audioTime, audioPlaying, { seek: true });
  }

  function reset() {
    clearVideo();
    setOffset(0);
    setLoop(false);
    setVolume(1);
  }

  const eventOptions = { signal: listeners.signal };
  function refreshMetadata() {
    renderTimelineOverlay();
    syncFromAudio(audioTime, audioPlaying, { seek: true });
  }

  video.addEventListener("loadedmetadata", refreshMetadata, eventOptions);
  video.addEventListener("durationchange", refreshMetadata, eventOptions);
  video.addEventListener("error", () => {
    if (!sourceUrl || !video.error) return;
    const error = new Error(video.error.message || "Unable to play the selected video.");
    clearVideo();
    onError(error);
  }, eventOptions);

  // The timeline's capture handlers should ignore events originating on this overlay.
  overlay?.addEventListener("pointerdown", event => {
    if (drag || event.button !== 0 || !duration()) return;
    const scale = getTimelineScale?.();
    if (!Number.isFinite(scale) || scale <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    overlay.setPointerCapture(event.pointerId);
    drag = { pointerId: event.pointerId, x: event.clientX, offset, scale };
  }, eventOptions);

  overlay?.addEventListener("pointermove", event => {
    if (drag?.pointerId !== event.pointerId) return;
    event.stopPropagation();
    setOffset(drag.offset + (event.clientX - drag.x) / drag.scale);
  }, eventOptions);

  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    overlay?.addEventListener(type, event => {
      if (drag?.pointerId !== event.pointerId) return;
      event.stopPropagation();
      drag = null;
    }, eventOptions);
  }

  renderTimelineOverlay();
  return {
    snapshot() {
      return sourceFile ? { file: sourceFile, offset, loop, volume: video.volume } : null;
    },
    loadVideo,
    clearVideo,
    setActive,
    setVolume,
    setOffset,
    setLoop,
    syncFromAudio,
    pause,
    reset,
    renderTimelineOverlay,
    dispose() {
      if (drag && overlay.hasPointerCapture(drag.pointerId)) {
        overlay.releasePointerCapture(drag.pointerId);
      }
      drag = null;
      listeners.abort();
      clearVideo();
    },
  };
}

function waitForMedia(video, type, signal) {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const events = new AbortController();
    const timer = setTimeout(() => finish(new Error("The export video did not load.")), 15000);
    function finish(error) {
      clearTimeout(timer);
      events.abort();
      if (error) reject(error);
      else resolve();
    }
    const options = { once: true, signal: events.signal };
    video.addEventListener(type, () => finish(), options);
    video.addEventListener("error", () => finish(new Error("The selected video cannot be decoded.")), options);
    signal.addEventListener("abort", () => finish(signal.reason), options);
  });
}

function drawExportLyrics(ctx, lines, time, width, height) {
  const line = lines.find(item => time >= item.start && time <= item.end);
  if (!line?.words.length) return;
  const size = Math.max(14, Math.round(height * 0.065));
  ctx.font = `bold ${size}px "Segoe UI", sans-serif`;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.lineWidth = Math.max(2, size * 0.08);
  ctx.strokeStyle = "#101010";
  const rows = [[]];
  let rowWidth = 0;
  for (const word of line.words) {
    const text = String(word.word).trim() + " ";
    const pixels = ctx.measureText(text).width;
    if (rowWidth + pixels > width * 0.9 && rowWidth) {
      rows.push([]);
      rowWidth = 0;
    }
    rows.at(-1).push({ text, pixels, start: word.start });
    rowWidth += pixels;
  }
  for (const [index, row] of rows.entries()) {
    let x = (width - row.reduce((sum, word) => sum + word.pixels, 0)) / 2;
    const y = height * 0.8 + (index - (rows.length - 1) / 2) * size * 1.2;
    for (const word of row) {
      ctx.fillStyle = time >= word.start ? "#c8ff70" : "#ffffff";
      ctx.strokeText(word.text, x, y);
      ctx.fillText(word.text, x, y);
      x += word.pixels;
    }
  }
}

export async function exportKaraokeMp4({
  source, prepareAudio, lyrics, start, end, signal, onProgress,
}) {
  const mimeType = "video/mp4;codecs=avc1,mp4a.40.2";
  if (!globalThis.MediaRecorder?.isTypeSupported(mimeType)) {
    throw new Error("This WebView2 Runtime does not support H.264/AAC MP4 export.");
  }
  if (!(end > start)) throw new Error("There is no audio range to export.");
  const video = document.createElement("video");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const audio = new AudioContext();
  const url = URL.createObjectURL(source.file);
  let stream;
  let recorder;
  let frame;
  try {
    await audio.resume();
    const metadata = waitForMedia(video, "loadeddata", signal);
    video.src = url;
    await metadata;
    if (!video.videoWidth || !(video.duration > 0)) throw new Error("The file has no playable video track.");
    const ratio = Math.min(1, 1920 / video.videoWidth, 1080 / video.videoHeight);
    canvas.width = Math.max(2, Math.round(video.videoWidth * ratio / 2) * 2);
    canvas.height = Math.max(2, Math.round(video.videoHeight * ratio / 2) * 2);

    const mix = await prepareAudio();
    signal.throwIfAborted();
    if (!mix.buffer || !(mix.buffer.duration > start)) throw new Error("MyNus audio is unavailable for export.");
    const sink = audio.createMediaStreamDestination();
    const compressor = audio.createDynamicsCompressor();
    compressor.connect(sink);
    const backing = audio.createBufferSource();
    backing.buffer = mix.buffer;
    const backingGain = audio.createGain();
    backingGain.gain.value = mix.volume;
    backing.connect(backingGain).connect(compressor);
    const videoGain = audio.createGain();
    videoGain.gain.value = source.volume;
    audio.createMediaElementSource(video).connect(videoGain).connect(compressor);

    const render = time => {
      const elapsed = time - source.offset;
      const visible = elapsed >= 0 && (source.loop || elapsed < video.duration);
      ctx.fillStyle = "black";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (visible && video.readyState >= 2) ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      drawExportLyrics(ctx, lyrics, time, canvas.width, canvas.height);
    };
    const initialTime = start - source.offset;
    video.loop = source.loop;
    if (initialTime >= 0 && (source.loop || initialTime < video.duration)) {
      const position = initialTime % video.duration;
      if (position > 0.001) {
        const seeked = waitForMedia(video, "seeked", signal);
        video.currentTime = position;
        await seeked;
      }
      await video.play();
    }
    render(start);

    stream = canvas.captureStream(30);
    stream.addTrack(sink.stream.getAudioTracks()[0]);
    recorder = new MediaRecorder(stream, { mimeType });
    const chunks = [];
    const events = new AbortController();
    try {
      await new Promise((resolve, reject) => {
        const options = { signal: events.signal };
        recorder.addEventListener("dataavailable", event => {
          if (event.data.size) chunks.push(event.data);
        }, options);
        recorder.addEventListener("stop", resolve, options);
        recorder.addEventListener("error", event => reject(event.error || new Error("MP4 encoding failed.")), options);
        signal.addEventListener("abort", () => reject(signal.reason), options);
        document.addEventListener("visibilitychange", () => {
          if (document.hidden) reject(new Error("Keep the MyNus window visible while exporting MP4."));
        }, options);
        if (signal.aborted) return reject(signal.reason);
        if (document.hidden) return reject(new Error("Open the MyNus window to export MP4."));

        recorder.start(1000);
        const origin = audio.currentTime;
        backing.start(origin, start, end - start);
        function tick() {
          try {
            const time = Math.min(end, start + audio.currentTime - origin);
            const elapsed = time - source.offset;
            const visible = elapsed >= 0 && (source.loop || elapsed < video.duration);
            if (visible && video.paused) {
              video.currentTime = elapsed % video.duration;
              video.play().catch(reject);
            } else if (!visible && !video.paused) {
              video.pause();
            } else if (visible && !video.seeking) {
              const target = elapsed % video.duration;
              if (Math.abs(video.currentTime - target) > 0.16) video.currentTime = target;
            }
            render(time);
            onProgress?.((time - start) / (end - start));
            if (time < end) frame = requestAnimationFrame(tick);
            else {
              video.pause();
              recorder.stop();
            }
          } catch (error) {
            reject(error);
          }
        }
        frame = requestAnimationFrame(tick);
      });
    } finally {
      events.abort();
    }
    if (!chunks.length) throw new Error("The MP4 encoder returned no video.");
    return new Blob(chunks, { type: "video/mp4" });
  } finally {
    cancelAnimationFrame(frame);
    if (recorder && recorder.state !== "inactive") recorder.stop();
    stream?.getTracks().forEach(track => track.stop());
    video.pause();
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
    await audio.close();
  }
}
