/*
 * 9.0.0
 */


const VIDEO_DRIFT_SECONDS = 0.2;
const VIDEO_CORRECTION_INTERVAL_MS = 500;

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
    overlay.title = `Video: ${length.toFixed(2)} s · Offset: ${offset.toFixed(2)} s`;
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
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(oldUrl);
    }
    playbackError = null;
    previousTarget = previousCycle = null;
    renderTimelineOverlay();
  }

  function loadVideo(file) {
    const nextUrl = URL.createObjectURL(file);
    clearVideo();
    sourceUrl = nextUrl;
    video.src = nextUrl;
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
