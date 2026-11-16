/*
 * 9.0.5 Save Video fix
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

function mp4Box(type, ...parts) {
  const header = new Uint8Array(8);
  new DataView(header.buffer).setUint32(0, 8 + parts.reduce((sum, part) => sum + part.size, 0));
  header.set(new TextEncoder().encode(type), 4);
  return new Blob([header, ...parts]);
}

function mp4Table(type, rows, columns, version = 0) {
  const bytes = new DataView(new ArrayBuffer(8 + rows.length * columns * 4));
  bytes.setUint8(0, version);
  bytes.setUint32(4, rows.length);
  let pos = 8;
  for (const row of rows) {
    for (const value of row) {
      bytes.setUint32(pos, value);
      pos += 4;
    }
  }
  return mp4Box(type, new Blob([bytes]));
}

function mp4Runs(values) {
  const rows = [];
  for (const value of values) {
    const last = rows.at(-1);
    if (last?.[1] === value) last[0]++;
    else rows.push([1, value]);
  }
  return rows;
}

function mp4Duration(bytes, at, value) {
  if (bytes.getUint8(0) === 1) bytes.setBigUint64(at, BigInt(value));
  else bytes.setUint32(at, value);
}

function readRecorderFragment(data, fragmentStart, tracks, media) {
  const root = { start: 0, end: data.byteLength };
  for (const traf of childBoxes(data, root.start, root.end)) {
    if (traf.type !== "traf") continue;
    const runs = Array.from(childBoxes(data, traf.start, traf.end))
      .filter(box => box.type === "trun" && data.getUint32(box.start + 4) > 0);
    if (!runs.length) continue;
    const header = childBox(data, traf, "tfhd");
    const decodeTime = childBox(data, traf, "tfdt");
    if (!header) throw new Error("The MP4 fragment has no track header.");
    let pos = header.start;
    const flags = data.getUint32(pos) & 0xffffff;
    const track = tracks.get(data.getUint32(pos + 4));
    if (!track) throw new Error("The MP4 fragment refers to an unknown track.");
    pos += 8;
    const base = flags & 1 ? Number(data.getBigUint64(pos)) : fragmentStart;
    if (flags & 1) pos += 8;
    if (flags & 2) pos += 4;
    const duration = flags & 8 ? data.getUint32(pos) : track.defaults.duration;
    if (flags & 8) pos += 4;
    const size = flags & 16 ? data.getUint32(pos) : track.defaults.size;
    if (flags & 16) pos += 4;
    const sampleFlags = flags & 32 ? data.getUint32(pos) : track.defaults.flags;
    if (decodeTime) {
      const at = decodeTime.start;
      const time = data.getUint8(at) === 1 ? Number(data.getBigUint64(at + 4)) : data.getUint32(at + 4);
      if (!track.durations.length) track.start = time;
      else if (time !== track.end) {
        const corrected = track.durations.at(-1) + time - track.end;
        if (corrected <= 0) throw new Error("The MP4 fragment timestamps overlap.");
        track.durations[track.durations.length - 1] = corrected;
      }
      track.end = time;
    }
    let dataOffset = base;
    for (const run of runs) {
      const version = data.getUint8(run.start);
      const fields = data.getUint32(run.start) & 0xffffff;
      const count = data.getUint32(run.start + 4);
      pos = run.start + 8;
      if (fields & 1) {
        dataOffset = base + data.getInt32(pos);
        pos += 4;
      }
      const firstFlags = fields & 4 ? data.getUint32(pos) : sampleFlags;
      if (fields & 4) pos += 4;
      let runSize = 0;
      for (let i = 0; i < count; i++) {
        const ticks = fields & 0x100 ? data.getUint32(pos) : duration;
        if (fields & 0x100) pos += 4;
        const bytes = fields & 0x200 ? data.getUint32(pos) : size;
        if (fields & 0x200) pos += 4;
        const status = fields & 0x400 ? data.getUint32(pos) : i === 0 ? firstFlags : sampleFlags;
        if (fields & 0x400) pos += 4;
        let composition = 0;
        if (fields & 0x800) {
          composition = version === 1 ? data.getInt32(pos) : data.getUint32(pos);
          pos += 4;
        }
        if (pos > run.end || !bytes) throw new Error("The MP4 contains an invalid sample.");
        track.durations.push(ticks);
        track.sizes.push(bytes);
        track.composition.push(composition);
        if (!(status & 0x10000)) track.sync.push([track.sizes.length]);
        track.end += ticks;
        runSize += bytes;
      }
      track.chunks.push({ offset: media.size, count });
      media.parts.push({ start: dataOffset, end: dataOffset + runSize });
      media.size += runSize;
      dataOffset += runSize;
    }
  }
}

export async function finalizeKaraokeMp4(recording, signal) {
  let fileType;
  let movie;
  let fragmented = false;
  const tracks = new Map();
  const media = { parts: [], size: 0 };
  const payloads = [];
  for (let pos = 0; recording.size - pos >= 8;) {
    signal?.throwIfAborted();
    const header = new DataView(await recording.slice(pos, pos + 16).arrayBuffer());
    const box = readBox(header, pos, recording.size);
    if (!box) throw new Error("The MP4 encoder produced an invalid container.");
    const boxStart = pos;
    pos = box.end;
    if (box.type === "ftyp") fileType = recording.slice(boxStart, box.end);
    if (box.type === "mdat") payloads.push(box);
    if (box.type === "moov") {
      movie = new DataView(await recording.slice(box.start, box.end).arrayBuffer());
      const root = { start: 0, end: movie.byteLength };
      const extension = childBox(movie, root, "mvex");
      if (!extension) continue;
      for (const entry of childBoxes(movie, extension.start, extension.end)) {
        if (entry.type !== "trex") continue;
        const at = entry.start;
        tracks.set(movie.getUint32(at + 4), {
          defaults: { duration: movie.getUint32(at + 12), size: movie.getUint32(at + 16), flags: movie.getUint32(at + 20) },
          start: 0, end: 0, durations: [], sizes: [], composition: [], sync: [], chunks: [],
        });
      }
    }
    if (box.type === "moof") {
      fragmented = true;
      const fragment = new DataView(await recording.slice(box.start, box.end).arrayBuffer());
      readRecorderFragment(fragment, boxStart, tracks, media);
    }
  }
  if (!fragmented && movie && fileType) return recording;
  if (!fileType || !movie || !media.parts.length) throw new Error("The MP4 encoder returned no usable samples.");
  const root = { start: 0, end: movie.byteLength };
  const mvhd = childBox(movie, root, "mvhd");
  const clock = new DataView(movie.buffer.slice(mvhd.start, mvhd.end));
  const movieScaleAt = clock.getUint8(0) === 1 ? 20 : 12;
  const movieScale = clock.getUint32(movieScaleAt);
  let movieDuration = 0;
  const copy = box => mp4Box(box.type, new Blob([movie.buffer.slice(box.start, box.end)]));
  const replaceChildren = (box, replace) =>
    mp4Box(box.type, ...Array.from(childBoxes(movie, box.start, box.end), replace).filter(Boolean));

  function makeMovie(mediaStart) {
    movieDuration = 0;
    const newTracks = [];
    for (const trak of childBoxes(movie, 0, movie.byteLength)) {
      if (trak.type !== "trak") continue;
      const tkhd = childBox(movie, trak, "tkhd");
      const trackHeader = new DataView(movie.buffer.slice(tkhd.start, tkhd.end));
      const trackIdAt = trackHeader.getUint8(0) === 1 ? 20 : 12;
      const track = tracks.get(trackHeader.getUint32(trackIdAt));
      if (!track?.sizes.length) continue;
      const mdia = childBox(movie, trak, "mdia");
      const mdhd = childBox(movie, mdia, "mdhd");
      const mediaHeader = new DataView(movie.buffer.slice(mdhd.start, mdhd.end));
      const rateAt = mediaHeader.getUint8(0) === 1 ? 20 : 12;
      const scale = mediaHeader.getUint32(rateAt);
      const duration = track.durations.reduce((sum, ticks) => sum + ticks, 0);
      const trackDuration = Math.round(duration * movieScale / scale);
      const delay = Math.round(track.start * movieScale / scale);
      mp4Duration(mediaHeader, rateAt + 4, duration);
      mp4Duration(trackHeader, trackIdAt + 8, delay + trackDuration);
      movieDuration = Math.max(movieDuration, delay + trackDuration);

      const stsc = [];
      for (const [i, chunk] of track.chunks.entries()) {
        if (stsc.at(-1)?.[1] !== chunk.count) stsc.push([i + 1, chunk.count, 1]);
      }
      const sizes = new DataView(new ArrayBuffer(12 + track.sizes.length * 4));
      sizes.setUint32(8, track.sizes.length);
      track.sizes.forEach((size, i) => sizes.setUint32(12 + i * 4, size));
      const bigOffsets = media.size + mediaStart > 0xffffffff;
      const offsets = track.chunks.map(chunk => {
        const absolute = mediaStart + chunk.offset;
        return bigOffsets ? [Math.floor(absolute / 2 ** 32), absolute >>> 0] : [absolute];
      });
      const sampleTables = [
        mp4Table("stts", mp4Runs(track.durations), 2),
        mp4Table("stsc", stsc, 3),
        mp4Box("stsz", new Blob([sizes])),
        mp4Table(bigOffsets ? "co64" : "stco", offsets, bigOffsets ? 2 : 1),
      ];
      if (track.sync.length < track.sizes.length) sampleTables.push(mp4Table("stss", track.sync, 1));
      if (track.composition.some(Boolean)) {
        sampleTables.push(mp4Table("ctts", mp4Runs(track.composition), 2, track.composition.some(value => value < 0) ? 1 : 0));
      }
      const newMedia = replaceChildren(mdia, entry => {
        if (entry.type === "mdhd") return mp4Box("mdhd", new Blob([mediaHeader]));
        if (entry.type !== "minf") return copy(entry);
        return replaceChildren(entry, item => {
          if (item.type !== "stbl") return copy(item);
          return mp4Box("stbl", copy(childBox(movie, item, "stsd")), ...sampleTables);
        });
      });
      const edits = delay ? mp4Box("edts", mp4Table("elst", [[delay, -1, 0x10000], [trackDuration, 0, 0x10000]], 3)) : null;
      newTracks.push(mp4Box("trak", mp4Box("tkhd", new Blob([trackHeader])), ...(edits ? [edits] : []), newMedia));
    }
    mp4Duration(clock, movieScaleAt + 4, movieDuration);
    return mp4Box("moov", mp4Box("mvhd", new Blob([clock])), ...newTracks);
  }

  const dataHeader = new Uint8Array(media.size + 8 > 0xffffffff ? 16 : 8);
  const dataView = new DataView(dataHeader.buffer);
  dataView.setUint32(0, dataHeader.length === 8 ? media.size + 8 : 1);
  dataHeader.set([0x6d, 0x64, 0x61, 0x74], 4);
  if (dataHeader.length === 16) dataView.setBigUint64(8, BigInt(media.size + 16));
  const headerSize = fileType.size + dataHeader.length;
  let info = makeMovie(0);
  // Recompute chunk offsets once the final movie header layout is known.
  let next = makeMovie(headerSize + info.size);
  if (next.size !== info.size) next = makeMovie(headerSize + next.size);
  info = next;
  let payloadIndex = 0;
  const parts = media.parts.map(part => {
    if (part.start < payloads[payloadIndex]?.start) payloadIndex = 0;
    while (payloads[payloadIndex]?.end <= part.start) payloadIndex++;
    const payload = payloads[payloadIndex];
    if (!payload || part.start < payload.start || part.end > payload.end) {
      throw new Error("An MP4 sample points outside the encoded media.");
    }
    return recording.slice(part.start, part.end);
  });
  return new Blob([fileType, info, dataHeader, ...parts], { type: "video/mp4" });
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
    onProgress?.(1, "finalizing");
    return await finalizeKaraokeMp4(new Blob(chunks, { type: "video/mp4" }), signal);
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
