// ============================================================
//  Video Station — QA Browser Console Snippets
//  Paste into DevTools Console while Video Station is open.
// ============================================================

// ─────────────────────────────────────────────────────────────
// 1. TIME TO FIRST FRAME (Time to Start)
//    Measures: click Play → first decoded video frame.
// ─────────────────────────────────────────────────────────────
(function measureTimeToStart() {
  const video = document.querySelector('#vs-player-video');
  if (!video) { console.error('Player not open — open a video first.'); return; }

  let t0 = null;
  const onPlay = () => { t0 = performance.now(); console.log('[TTS] Play clicked at', t0.toFixed(0), 'ms'); };
  const onPlaying = () => {
    if (t0 === null) return;
    const tts = performance.now() - t0;
    console.log(`%c[TTS] ✅ Time to First Frame: ${tts.toFixed(0)} ms`, 'color:lime;font-weight:bold');
    // Benchmark:
    if (tts < 1000) console.log('%c  EXCELLENT (< 1s)', 'color:lime');
    else if (tts < 3000) console.log('%c  GOOD (< 3s)', 'color:orange');
    else console.log('%c  SLOW (> 3s) — check HLS preset', 'color:red');
    video.removeEventListener('play', onPlay);
    video.removeEventListener('playing', onPlaying);
  };
  video.addEventListener('play', onPlay);
  video.addEventListener('playing', onPlaying);
  console.log('[TTS] Monitoring started. Press Play to measure.');
})();


// ─────────────────────────────────────────────────────────────
// 2. BUFFER HEALTH MONITOR
//    Shows buffered seconds ahead of current position (live).
// ─────────────────────────────────────────────────────────────
(function startBufferMonitor() {
  const video = document.querySelector('#vs-player-video');
  if (!video) { console.error('Player not open.'); return; }

  let timer = null;
  const log = () => {
    const pos = video.currentTime;
    let ahead = 0;
    for (let i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= pos && video.buffered.end(i) >= pos) {
        ahead = video.buffered.end(i) - pos;
        break;
      }
    }
    const bar = '█'.repeat(Math.min(30, Math.floor(ahead / 2)));
    const color = ahead >= 30 ? 'color:lime' : ahead >= 10 ? 'color:orange' : 'color:red';
    console.log(`%c[BUF] ${pos.toFixed(1)}s | ahead: ${ahead.toFixed(1)}s ${bar}`, color);
  };
  timer = setInterval(log, 2000);
  window._vsStopBufMonitor = () => { clearInterval(timer); console.log('[BUF] Stopped.'); };
  console.log('[BUF] Buffer monitor started. Call window._vsStopBufMonitor() to stop.');
})();


// ─────────────────────────────────────────────────────────────
// 3. SEEK LATENCY TEST
//    Measures how long after seek() the video resumes playing.
// ─────────────────────────────────────────────────────────────
(function measureSeekLatency() {
  const video = document.querySelector('#vs-player-video');
  if (!video) { console.error('Player not open.'); return; }

  let seekStart = null;
  video.addEventListener('seeking', () => { seekStart = performance.now(); });
  video.addEventListener('seeked',  () => {
    if (seekStart === null) return;
    const lat = performance.now() - seekStart;
    const color = lat < 500 ? 'color:lime' : lat < 2000 ? 'color:orange' : 'color:red';
    console.log(`%c[SEEK] Latency: ${lat.toFixed(0)} ms`, color);
    seekStart = null;
  });
  console.log('[SEEK] Monitoring seek latency. Use player scrubber to test.');
})();


// ─────────────────────────────────────────────────────────────
// 4. HEARTBEAT STRESS TEST
//    Fires 20 heartbeats in rapid succession (tests backend).
// ─────────────────────────────────────────────────────────────
(async function stressHeartbeat() {
  const token = NAS && NAS.token;
  // Find active HLS session from URL patterns
  const video = document.querySelector('#vs-player-video');
  if (!video || !video.src) { console.error('No active HLS session.'); return; }

  // Extract session_id from playlist URL
  const match = video.src.match(/hls\/([^/]+)\/playlist/);
  if (!match) { console.error('Could not find session_id.'); return; }
  const sid = match[1];

  let ok = 0, fail = 0;
  for (let i = 0; i < 20; i++) {
    const r = await fetch(`/api/video-station/hls/${sid}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({ pos: video.currentTime + i })
    });
    r.ok ? ok++ : fail++;
    await new Promise(r => setTimeout(r, 50));
  }
  console.log(`%c[HB STRESS] ✅ ${ok}/20 OK, ❌ ${fail}/20 FAIL`,
    fail === 0 ? 'color:lime;font-weight:bold' : 'color:red;font-weight:bold');
})();


// ─────────────────────────────────────────────────────────────
// 5. CONTROLS AUTO-HIDE TEST
//    Checks that controls disappear after 3s of inactivity.
// ─────────────────────────────────────────────────────────────
(function testControlsHide() {
  const overlay = document.querySelector('#vs-player-overlay');
  if (!overlay) { console.error('Player not open.'); return; }

  const controls = overlay.querySelector('.vs-player-controls, .vs-player-top');
  if (!controls) { console.warn('Could not find controls element.'); return; }

  const getOpacity = () => parseFloat(getComputedStyle(controls).opacity);
  const vis0 = getOpacity();
  console.log('[CTRL] Initial opacity:', vis0);

  setTimeout(() => {
    const vis3 = getOpacity();
    const color = vis3 < 0.1 ? 'color:lime' : 'color:red';
    console.log(`%c[CTRL] After 3.5s: opacity = ${vis3} (should be ~0 if auto-hide works)`, color);
  }, 3500);

  console.log('[CTRL] Do NOT move mouse for 3.5s...');
})();


// ─────────────────────────────────────────────────────────────
// 6. STALE SESSION DETECTOR
//    Checks /tmp for leftover vs_hls_* dirs (ghost processes).
//    Works via backend watcher-status endpoint.
// ─────────────────────────────────────────────────────────────
(async function detectGhostSessions() {
  const token = NAS && NAS.token;
  const r = await fetch(`/api/video-station/watcher-status`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  const d = await r.json();
  console.log('[GHOST] Watcher status:', JSON.stringify(d, null, 2));
  console.log('[GHOST] To check /tmp directly, run on server: ls /tmp/vs_hls_*');
})();
