"""Mobile full-duplex Gemini Live HTML UI for Watch Bridge (/live)."""

LIVE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>Watch Bridge Live</title>
  <style>
    :root {
      --night: #140a24;
      --card: #1f1335;
      --rim: #523478;
      --ink: #ece4f8;
      --dim: #a68fc7;
      --gold: #ffd9a0;
      --lilac: #c9a0ff;
      --mint: #9fe6b8;
      --coral: #ff8b8b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      background: radial-gradient(circle at 50% 18%, #2e1b52 0%, var(--night) 68%);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex;
      flex-direction: column;
      padding: env(safe-area-inset-top, 16px) 18px env(safe-area-inset-bottom, 20px);
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 0;
      font-size: 0.9rem;
      color: var(--dim);
    }
    header a { color: var(--lilac); text-decoration: none; }
    .stage {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 20px;
      text-align: center;
    }
    .orb-btn {
      width: 168px;
      height: 168px;
      border-radius: 50%;
      border: 2px solid var(--rim);
      background: radial-gradient(circle at 38% 32%, #4a2c7a, #1d1033 72%);
      color: var(--gold);
      font-size: 1.08rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      cursor: pointer;
      box-shadow: 0 0 34px rgba(201, 160, 255, 0.22);
      transition: transform 0.18s ease, box-shadow 0.25s ease, border-color 0.25s ease;
    }
    .orb-btn.live {
      border-color: var(--mint);
      color: var(--mint);
      box-shadow: 0 0 48px rgba(159, 230, 184, 0.38);
      animation: breathe 2.4s ease-in-out infinite;
    }
    .orb-btn.speaking {
      border-color: var(--gold);
      color: var(--gold);
      box-shadow: 0 0 56px rgba(255, 217, 160, 0.5);
    }
    @keyframes breathe {
      0%, 100% { transform: scale(1); }
      50% { transform: scale(1.045); }
    }
    .status-line {
      font-size: 0.95rem;
      color: var(--dim);
      min-height: 1.4em;
    }
    .pills {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 6px;
      max-width: 520px;
    }
    .pill {
      font-family: ui-monospace, Menlo, monospace;
      font-size: 0.76rem;
      background: var(--card);
      border: 1px solid var(--rim);
      border-radius: 999px;
      padding: 3px 10px;
      color: var(--lilac);
    }
    .pill.done { border-color: var(--mint); color: var(--mint); }
    .pill.fail { border-color: var(--coral); color: var(--coral); }
    .transcript-box {
      width: 100%;
      max-width: 540px;
      min-height: 96px;
      max-height: 220px;
      overflow-y: auto;
      background: rgba(31, 19, 53, 0.78);
      border: 1px solid var(--rim);
      border-radius: 14px;
      padding: 12px 14px;
      text-align: left;
      font-size: 0.94rem;
      line-height: 1.45;
    }
    .turn-you { color: var(--dim); margin-bottom: 6px; }
    .turn-ai { color: var(--ink); margin-bottom: 8px; }
    .controls {
      display: flex;
      gap: 12px;
      justify-content: center;
      padding-top: 8px;
    }
    .ctrl-btn {
      background: var(--card);
      color: var(--ink);
      border: 1px solid var(--rim);
      border-radius: 999px;
      padding: 10px 20px;
      font-size: 0.9rem;
      cursor: pointer;
    }
    .ctrl-btn.danger { border-color: var(--coral); color: var(--coral); }
    .hidden { display: none; }
  </style>
</head>
<body>
  <header>
    <span>Watch Bridge · Gemini Live</span>
    <a href="/app">Dashboard</a>
  </header>

  <div class="stage">
    <button id="orb" class="orb-btn" type="button">Tap to Talk</button>
    <div id="status" class="status-line">Ready for full-duplex voice</div>
    <div id="pills" class="pills"></div>
    <div id="transcript" class="transcript-box">
      <div class="turn-you">Tap the orb to open your microphone and talk naturally across Trello, Calendar, Home Assistant, and Carbon.</div>
    </div>
    <div id="controls" class="controls hidden">
      <button id="mutebtn" class="ctrl-btn" type="button">Mute Mic</button>
      <button id="endbtn" class="ctrl-btn danger" type="button">End Call</button>
    </div>
  </div>

  <script>
    (() => {
      // Save bearer token from URL fragment (#token=...) into localStorage once,
      // then scrub the URL bar so the token never lingers in browser history.
      if (location.hash.startsWith("#token=")) {
        const rawTok = decodeURIComponent(location.hash.slice(7)).trim();
        if (rawTok) {
          localStorage.setItem("bridge_live_token", rawTok);
        }
        history.replaceState(null, "", "/live");
      }

      const WORKLET_SOURCE = `
        class BridgeMicCaptureProcessor extends AudioWorkletProcessor {
          constructor() {
            super();
            this._carry = [];
            this._targetRate = 16000;
          }
          process(inputs) {
            const ch = inputs[0] && inputs[0][0];
            if (!ch || !ch.length) return true;
            const ratio = sampleRate / this._targetRate;
            let pos = 0;
            while (pos < ch.length) {
              const idx = Math.min(ch.length - 1, Math.floor(pos));
              const s = Math.max(-1, Math.min(1, ch[idx]));
              this._carry.push(s < 0 ? s * 0x8000 : s * 0x7fff);
              pos += ratio;
            }
            if (this._carry.length >= 1600) {
              const chunk = new Int16Array(this._carry.splice(0, 1600));
              this.port.postMessage(chunk.buffer, [chunk.buffer]);
            }
            return true;
          }
        }
        registerProcessor("bridge-mic-capture", BridgeMicCaptureProcessor);
      `;

      const orb = document.getElementById("orb");
      const statusEl = document.getElementById("status");
      const pillsEl = document.getElementById("pills");
      const transcriptEl = document.getElementById("transcript");
      const controlsEl = document.getElementById("controls");
      const muteBtn = document.getElementById("mutebtn");
      const endBtn = document.getElementById("endbtn");

      let ws = null;
      let audioCtx = null;
      let micStream = null;
      let workletNode = null;
      let muted = false;
      let activeSources = [];
      let nextPlayAt = 0;
      let idleTimer = null;
      let aiSpan = null;
      let userSpan = null;

      function resetIdleWatchdog() {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          setStatus("Hung up after 2 minutes of inactivity");
          hangUp();
        }, 120000);
      }

      function setStatus(msg) {
        statusEl.textContent = msg;
      }

      function bytesToBase64(buf) {
        const bytes = new Uint8Array(buf);
        let bin = "";
        for (let i = 0; i < bytes.length; i++) {
          bin += String.fromCharCode(bytes[i]);
        }
        return btoa(bin);
      }

      function playPcm24k(b64) {
        if (!audioCtx) return;
        const bin = atob(b64);
        const sampleCount = bin.length >> 1;
        if (!sampleCount) return;
        const audioBuf = audioCtx.createBuffer(1, sampleCount, 24000);
        const channel = audioBuf.getChannelData(0);
        for (let i = 0; i < sampleCount; i++) {
          const lo = bin.charCodeAt(i * 2);
          const hi = bin.charCodeAt(i * 2 + 1);
          let val = (hi << 8) | lo;
          if (val >= 0x8000) val -= 0x10000;
          channel[i] = val / 32768;
        }
        const src = audioCtx.createBufferSource();
        src.buffer = audioBuf;
        src.connect(audioCtx.destination);
        const startAt = Math.max(audioCtx.currentTime + 0.015, nextPlayAt);
        src.start(startAt);
        nextPlayAt = startAt + audioBuf.duration;
        orb.classList.add("speaking");
        activeSources.push(src);
        src.onended = () => {
          activeSources = activeSources.filter((x) => x !== src);
          if (!activeSources.length) orb.classList.remove("speaking");
        };
      }

      function flushSpeakerQueue() {
        for (const src of activeSources) {
          try { src.stop(); } catch (_) {}
        }
        activeSources = [];
        if (audioCtx) nextPlayAt = audioCtx.currentTime;
        orb.classList.remove("speaking");
      }

      function appendTranscript(kind, text) {
        if (!text) return;
        if (kind === "user") {
          aiSpan = null;
          if (!userSpan) {
            userSpan = document.createElement("div");
            userSpan.className = "turn-you";
            userSpan.textContent = "You: ";
            transcriptEl.appendChild(userSpan);
          }
          userSpan.textContent += text;
        } else {
          userSpan = null;
          if (!aiSpan) {
            aiSpan = document.createElement("div");
            aiSpan.className = "turn-ai";
            transcriptEl.appendChild(aiSpan);
          }
          aiSpan.textContent += text;
        }
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
      }

      function addToolPill(name, cls) {
        const el = document.createElement("span");
        el.className = "pill " + (cls || "");
        el.dataset.tool = name;
        el.textContent = name;
        pillsEl.appendChild(el);
        while (pillsEl.children.length > 6) {
          pillsEl.removeChild(pillsEl.firstChild);
        }
      }

      function markToolPill(name, ok) {
        const items = pillsEl.querySelectorAll(`.pill[data-tool="${CSS.escape(name)}"]`);
        const target = items[items.length - 1];
        if (target) target.classList.add(ok ? "done" : "fail");
      }

      async function startCall() {
        orb.disabled = true;
        transcriptEl.innerHTML = "";
        pillsEl.innerHTML = "";
        setStatus("Opening microphone...");

        try {
          // Must instantiate and resume AudioContext synchronously inside user gesture for iOS Safari.
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
          await audioCtx.resume();
          micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1,
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            }
          });
          const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
          const modUrl = URL.createObjectURL(blob);
          await audioCtx.audioWorklet.addModule(modUrl);
          URL.revokeObjectURL(modUrl);
        } catch (err) {
          setStatus("Microphone access denied: " + (err.message || err));
          orb.disabled = false;
          hangUp(true);
          return;
        }

        setStatus("Connecting to Watch Bridge...");
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + "/live/ws");

        ws.onopen = () => {
          const savedToken = localStorage.getItem("bridge_live_token") || "";
          ws.send(JSON.stringify({ type: "start", token: savedToken }));
        };

        ws.onmessage = (ev) => {
          resetIdleWatchdog();
          const msg = JSON.parse(ev.data);
          if (msg.type === "ready") {
            orb.disabled = false;
            orb.textContent = "Listening";
            orb.classList.add("live");
            controlsEl.classList.remove("hidden");
            setStatus("Live (" + (msg.voice || "Puck") + ") — speak anytime");

            const source = audioCtx.createMediaStreamSource(micStream);
            workletNode = new AudioWorkletNode(audioCtx, "bridge-mic-capture");
            workletNode.port.onmessage = (chunkEv) => {
              if (!muted && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                  type: "audio",
                  data: bytesToBase64(chunkEv.data),
                }));
              }
            };
            source.connect(workletNode);
          } else if (msg.type === "audio") {
            playPcm24k(msg.data);
          } else if (msg.type === "interrupted") {
            flushSpeakerQueue();
          } else if (msg.type === "user_transcript") {
            appendTranscript("user", msg.text);
          } else if (msg.type === "model_transcript") {
            appendTranscript("ai", msg.text);
          } else if (msg.type === "tool_call") {
            addToolPill(msg.name, "");
            setStatus("Running " + msg.name + "...");
          } else if (msg.type === "tool_result") {
            markToolPill(msg.name, msg.ok);
            setStatus("Listening...");
          } else if (msg.type === "turn_complete") {
            aiSpan = null;
            userSpan = null;
          } else if (msg.type === "error") {
            setStatus(msg.detail || "Live session error");
            hangUp(true);
          }
        };

        ws.onclose = () => {
          hangUp(true);
        };
      }

      function hangUp(preserveStatus) {
        if (idleTimer) {
          clearTimeout(idleTimer);
          idleTimer = null;
        }
        flushSpeakerQueue();
        if (workletNode) {
          try { workletNode.disconnect(); } catch (_) {}
          workletNode = null;
        }
        if (micStream) {
          micStream.getTracks().forEach((t) => t.stop());
          micStream = null;
        }
        if (audioCtx) {
          try { audioCtx.close(); } catch (_) {}
          audioCtx = null;
        }
        if (ws) {
          try {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: "stop" }));
            }
            ws.close();
          } catch (_) {}
          ws = null;
        }
        muted = false;
        muteBtn.textContent = "Mute Mic";
        orb.disabled = false;
        orb.classList.remove("live", "speaking");
        orb.textContent = "Tap to Talk";
        controlsEl.classList.add("hidden");
        if (!preserveStatus) {
          setStatus("Call ended");
        }
      }

      orb.addEventListener("click", () => {
        if (ws) {
          hangUp(false);
        } else {
          startCall();
        }
      });

      muteBtn.addEventListener("click", () => {
        muted = !muted;
        muteBtn.textContent = muted ? "Unmute Mic" : "Mute Mic";
        setStatus(muted ? "Microphone muted" : "Listening...");
      });

      endBtn.addEventListener("click", () => hangUp(false));
    })();
  </script>
</body>
</html>
"""

DASHBOARD_GEMINI_JS = """
function geminiCard(d) {
  const g = d.gemini_live || {};
  const voiceOpts = (g.voices || ['Puck', 'Aoede', 'Charon', 'Fenrir', 'Kore']).map(
    v => `<option value="${escA(v)}"${v === g.voice ? ' selected' : ''}>${esc(v)}</option>`
  ).join('');
  if (!g.connected) {
    return `<p class="sub">Run a live, interruptible Gemini voice call on your iPhone connected to your Trello, Calendar, Home Assistant, and Carbon tools. Paste a key from <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener" style="color:var(--accent)">Google AI Studio</a> below.</p>
    <input id="gemkey" type="password" placeholder="AIza... Gemini API key" autocomplete="off">
    <div class="row" style="margin-top:8px"><span class="sub">Voice</span><select id="gemvoice" style="flex:1">${voiceOpts}</select></div>
    <button onclick="connectGemini()">Connect Gemini Live</button> <span id="gemerr"></span>`;
  }
  return `<p class="sub"><span class="ok">connected</span> — ${esc(g.model || 'gemini-live')} · voice <b>${esc(g.voice || 'Puck')}</b></p>
  <p><a class="btn" href="/live">Open Live Conversation</a></p>
  <p class="sub"><b>iPhone Shortcut (Action Button):</b> create a 1-step Shortcut with <b>Open URLs</b> set to the address below. First launch stores your token locally and clears it from the address bar.</p>
  <div class="kv"><span class="k">Shortcut URL</span><span class="v">${esc(g.live_url)}</span><button class="sec" id="gemcopy">Copy</button></div>
  <div class="row" style="margin-top:10px"><span class="sub">Voice</span><select id="gemvoice" onchange="changeGeminiVoice()" style="flex:1">${voiceOpts}</select></div>
  <button class="sec" id="gemdisc" style="margin-top:10px">Disconnect</button> <span id="gemerr"></span>`;
}
async function connectGemini() {
  const err = document.getElementById('gemerr');
  err.textContent = '';
  const r = await fetch('/app/gemini', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: document.getElementById('gemkey').value,
      voice: document.getElementById('gemvoice').value
    })
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({ detail: 'error ' + r.status }));
    err.textContent = d.detail || ('error ' + r.status);
    return;
  }
  render(await r.json());
}
async function changeGeminiVoice() {
  const r = await fetch('/app/gemini', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: '', voice: document.getElementById('gemvoice').value })
  });
  if (r.ok) render(await r.json());
}
async function disconnectGemini() {
  if (!confirm('Disconnect your Gemini API key?')) return;
  const r = await fetch('/app/gemini/disconnect', { method: 'POST' });
  if (r.ok) render(await r.json());
}
"""

