"""Web UI for listing, searching, activating, and deleting Carbon projects (/projects)."""

PROJECTS_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>☾ Carbon Projects — Watch Bridge</title>
  <style>
    :root {
      --bg: #150b26;
      --panel: #1f1335;
      --border: #4a2e6b;
      --border2: #6b3fa0;
      --text: #e8dff5;
      --muted: #a891c9;
      --accent: #c9a0ff;
      --accent2: #ffd9a0;
      --green: #9fe6b8;
      --danger: #ff8fa3;
      --code: #120a20;
      --mono: ui-monospace, Menlo, monospace;
      --serif: Georgia, "Times New Roman", serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: var(--serif);
      line-height: 1.6;
      background:
        radial-gradient(ellipse at 20% -10%, #33205c 0%, transparent 55%),
        radial-gradient(ellipse at 85% 15%, #241542 0%, transparent 50%),
        radial-gradient(1.5px 1.5px at 12% 22%, #fff 50%, transparent 51%),
        radial-gradient(1px 1px at 32% 8%, #e8dff5 50%, transparent 51%),
        radial-gradient(1.5px 1.5px at 55% 30%, #fff 50%, transparent 51%),
        radial-gradient(1px 1px at 71% 12%, #ffd9a0 50%, transparent 51%),
        radial-gradient(2px 2px at 88% 34%, #fff 50%, transparent 51%),
        linear-gradient(180deg, #150b26 0%, #1a0f30 60%, #241542 100%);
      background-attachment: fixed;
    }
    main {
      max-width: 700px;
      margin: 0 auto;
      padding: 44px 20px 80px;
    }
    .top {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
    }
    h1 {
      font-size: 1.5rem;
      margin: 0 0 4px;
      letter-spacing: 0.06em;
      color: var(--accent2);
      text-shadow: 0 0 18px rgba(255, 217, 160, 0.35);
    }
    h1::before {
      content: "☾ ";
      color: var(--accent);
    }
    a { color: var(--accent); }
    .top a { color: var(--muted); font-size: 0.88rem; }
    .sub { color: var(--muted); font-size: 0.92rem; }
    .status-strip {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      background: rgba(31, 19, 53, 0.85);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 16px;
      margin: 14px 0;
      font-size: 0.88rem;
    }
    .toolbar {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin: 14px 0 18px;
    }
    input[type="search"] {
      width: 100%;
      background: var(--code);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 10px;
      padding: 11px 14px;
      font-family: var(--mono);
      font-size: 0.94rem;
    }
    input[type="search"]:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 10px rgba(201, 160, 255, 0.32);
    }
    .filters {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
    .pill-group {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    button.filter-pill {
      background: rgba(18, 10, 32, 0.75);
      border: 1px solid var(--border);
      color: var(--muted);
      border-radius: 999px;
      padding: 5px 14px;
      font-family: var(--mono);
      font-size: 0.78rem;
      cursor: pointer;
    }
    button.filter-pill.on {
      border-color: var(--accent);
      color: var(--accent2);
      background: rgba(107, 63, 160, 0.35);
    }
    .pcard {
      background: rgba(31, 19, 53, 0.88);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px 18px;
      margin: 14px 0;
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.32);
    }
    .pcard.active-card {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px rgba(201, 160, 255, 0.2), 0 6px 24px rgba(0, 0, 0, 0.38);
    }
    .phead {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      flex-wrap: wrap;
    }
    .ptitle {
      font-size: 1.08rem;
      font-weight: 700;
      color: var(--accent2);
    }
    .ppath {
      font-family: var(--mono);
      font-size: 0.78rem;
      color: var(--muted);
      margin-top: 2px;
    }
    .badges {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
    }
    .badge {
      font-family: var(--mono);
      font-size: 0.72rem;
      padding: 2px 9px;
      border-radius: 999px;
      border: 1px solid var(--border);
      color: var(--muted);
      background: var(--code);
    }
    .badge.active {
      border-color: var(--green);
      color: var(--green);
    }
    .badge.watch {
      border-color: var(--border2);
      color: var(--accent);
    }
    .psum {
      margin: 10px 0 8px;
      font-size: 0.94rem;
    }
    .platest {
      background: var(--code);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 11px;
      margin: 8px 0;
      font-size: 0.84rem;
      color: var(--muted);
    }
    .platest b { color: var(--text); font-weight: 600; }
    .pfoot {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .pmeta {
      font-family: var(--mono);
      font-size: 0.75rem;
      color: var(--muted);
    }
    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    button.act {
      background: linear-gradient(180deg, #c9a0ff, #a874e8);
      color: #1a0f30;
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 6px 14px;
      font-family: var(--serif);
      font-weight: 700;
      font-size: 0.84rem;
      cursor: pointer;
    }
    button.sec {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--muted);
      border-radius: 8px;
      padding: 5px 12px;
      font-family: var(--mono);
      font-size: 0.76rem;
      cursor: pointer;
    }
    button.danger-outline {
      background: transparent;
      border: 1px solid rgba(255, 143, 163, 0.55);
      color: var(--danger);
      border-radius: 8px;
      padding: 5px 12px;
      font-family: var(--mono);
      font-size: 0.76rem;
      cursor: pointer;
    }
    button.danger-solid {
      background: #c94a63;
      border: 1px solid var(--danger);
      color: #fff;
      border-radius: 8px;
      padding: 6px 14px;
      font-family: var(--mono);
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
    }
    .confirm-box {
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid rgba(255, 143, 163, 0.6);
      background: rgba(45, 14, 28, 0.65);
      font-size: 0.88rem;
    }
    .confirm-box label {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 8px 0 10px;
      font-family: var(--mono);
      font-size: 0.8rem;
      color: var(--accent2);
      cursor: pointer;
    }
    .confirm-box input[type="checkbox"] {
      width: 16px;
      height: 16px;
      accent-color: #ff8fa3;
    }
    .banner-msg {
      min-height: 1.3em;
      font-size: 0.88rem;
      margin: 6px 0;
    }
    .banner-msg.ok { color: var(--green); }
    .banner-msg.err { color: var(--danger); }
    .empty {
      text-align: center;
      padding: 28px 16px;
      color: var(--muted);
      border: 1px dashed var(--border);
      border-radius: 12px;
    }
    code { color: var(--accent2); font-family: var(--mono); }
    .orn {
      color: var(--accent);
      letter-spacing: 0.4em;
      text-align: center;
      margin: 24px 0 0;
      font-size: 0.8rem;
    }
    .foot {
      margin-top: 2.2rem;
      font-size: 0.8rem;
      text-align: center;
    }
    .foot a { color: var(--muted); }
  </style>
</head>
<body>
  <main>
    <div class="top">
      <h1>Carbon Projects</h1>
      <span>
        <span id="who" class="sub"></span> ·
        <a href="/app">← dashboard</a> ·
        <a href="/logout">log out</a>
      </span>
    </div>
    <p class="sub">
      List, search, switch, or remove coding projects managed through Watch Bridge.
    </p>

    <div class="status-strip">
      <div>
        Active workspace: <code id="activeSlug">…</code>
      </div>
      <div id="workerBadge" class="sub">checking workstation…</div>
    </div>

    <div class="toolbar">
      <input
        id="searchInput"
        type="search"
        placeholder="Find project by name, slug, or summary…"
        autocomplete="off"
      >
      <div class="filters">
        <div class="pill-group">
          <button id="pillAll" class="filter-pill on" type="button">All projects</button>
          <button id="pillWatch" class="filter-pill" type="button">Started via Watch</button>
        </div>
        <span id="countLabel" class="sub"></span>
      </div>
    </div>

    <div id="feedback" class="banner-msg"></div>
    <div id="projectList"></div>

    <p class="orn">✦ ☾ ✦</p>
    <p class="foot">
      <a href="/app">dashboard</a> ·
      <a href="/privacy">privacy</a> ·
      <a href="/health">status</a>
    </p>
  </main>

  <script>
    let allProjects = [];
    let watchOnly = false;
    let confirmingSlug = null;

    function esc(s) {
      const d = document.createElement('div');
      d.textContent = s || '';
      return d.innerHTML;
    }

    function escAttr(s) {
      return esc(s).replace(/"/g, '&quot;');
    }

    function setFeedback(text, isErr) {
      const box = document.getElementById('feedback');
      box.textContent = text || '';
      box.className = 'banner-msg ' + (text ? (isErr ? 'err' : 'ok') : '');
    }

    function matchesSearch(p, q) {
      if (!q) return true;
      const needle = q.toLowerCase().trim();
      const slugNeedle = needle.replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
      const hay = (
        (p.name || '') + ' ' +
        (p.slug || '') + ' ' +
        (p.summary || '') + ' ' +
        (p.latest_reply || '')
      ).toLowerCase();
      if (hay.includes(needle)) return true;
      return Boolean(slugNeedle && (p.slug || '').includes(slugNeedle));
    }

    function renderProjects() {
      const q = document.getElementById('searchInput').value || '';
      const filtered = allProjects.filter(p => {
        if (watchOnly && !p.started_via_watch) return false;
        return matchesSearch(p, q);
      });

      const watchCount = allProjects.filter(p => p.started_via_watch).length;
      document.getElementById('pillAll').textContent = 'All projects (' + allProjects.length + ')';
      document.getElementById('pillWatch').textContent = 'Started via Watch (' + watchCount + ')';
      document.getElementById('pillAll').classList.toggle('on', !watchOnly);
      document.getElementById('pillWatch').classList.toggle('on', watchOnly);
      document.getElementById('countLabel').textContent =
        filtered.length === allProjects.length
          ? filtered.length + ' shown'
          : filtered.length + ' of ' + allProjects.length + ' shown';

      const box = document.getElementById('projectList');
      if (!filtered.length) {
        box.innerHTML = '<div class="empty">No matching projects found.</div>';
        return;
      }

      box.innerHTML = filtered.map(p => {
        const badges = [];
        if (p.active) badges.push('<span class="badge active">✦ active</span>');
        if (p.started_via_watch) {
          badges.push('<span class="badge watch">⌚ watch-started</span>');
        } else {
          badges.push('<span class="badge">⌂ existing repo</span>');
        }
        if (p.has_thread) badges.push('<span class="badge">thread open</span>');

        const summaryHtml = p.summary
          ? '<div class="psum">' + esc(p.summary) + '</div>'
          : '<div class="psum sub">No summary recorded yet.</div>';

        const latestHtml = p.latest_reply
          ? '<div class="platest"><b>Latest reply:</b> ' + esc(p.latest_reply) + '</div>'
          : '';

        const updatedDate = (p.latest_at || p.updated_at || '').slice(0, 10);
        const turnsWord = p.turn_count === 1 ? '1 turn' : (p.turn_count + ' turns');

        let actionBtns = '';
        if (!p.active) {
          actionBtns += '<button class="act" type="button" data-act="activate" data-slug="' +
            escAttr(p.slug) + '">Set active</button>';
        }
        if (p.can_delete) {
          actionBtns += '<button class="danger-outline" type="button" data-act="ask-delete" data-slug="' +
            escAttr(p.slug) + '">Delete…</button>';
        } else {
          actionBtns += '<span class="pmeta">protected default</span>';
        }

        let confirmHtml = '';
        if (confirmingSlug === p.slug && p.can_delete) {
          const filesOpt = p.can_delete_files
            ? '<label><input type="checkbox" id="delFiles_' + escAttr(p.slug) + '"> ' +
              'Also delete workstation folder ' + esc(p.workspace_path) + '</label>'
            : '<p class="sub" style="margin:6px 0 10px">Pre-existing repository: only removes it from Watch Bridge (leaves ' +
              esc(p.workspace_path) + ' untouched on disk).</p>';

          confirmHtml =
            '<div class="confirm-box">' +
              '<div><b>Remove “' + esc(p.name) + '”?</b> This clears its project entry and turn history on Watch Bridge.</div>' +
              filesOpt +
              '<div class="actions">' +
                '<button class="danger-solid" type="button" data-act="confirm-delete" data-slug="' + escAttr(p.slug) + '">Confirm delete</button>' +
                '<button class="sec" type="button" data-act="cancel-delete">Cancel</button>' +
              '</div>' +
            '</div>';
        }

        return (
          '<div class="pcard' + (p.active ? ' active-card' : '') + '">' +
            '<div class="phead">' +
              '<div>' +
                '<div class="ptitle">' + esc(p.name) + '</div>' +
                '<div class="ppath">' + esc(p.workspace_path) + '</div>' +
              '</div>' +
              '<div class="badges">' + badges.join('') + '</div>' +
            '</div>' +
            summaryHtml +
            latestHtml +
            '<div class="pfoot">' +
              '<div class="pmeta">' + esc(turnsWord) + (updatedDate ? ' · updated ' + esc(updatedDate) : '') + '</div>' +
              '<div class="actions">' + actionBtns + '</div>' +
            '</div>' +
            confirmHtml +
          '</div>'
        );
      }).join('');
    }

    function applyPayload(d) {
      document.getElementById('who').textContent = d.username || '';
      const st = d.state || {};
      document.getElementById('activeSlug').textContent = '~/Code/' + (st.active_project || 'watch-llm-bridge');
      const wb = document.getElementById('workerBadge');
      if (d.worker_online) {
        wb.innerHTML = '<span style="color:var(--green)">●</span> worker online' +
          (st.worker_host ? ' (' + esc(st.worker_host) + ')' : '');
      } else {
        wb.textContent = '○ worker offline / idle';
      }
      allProjects = d.projects || [];
      renderProjects();
    }

    async function loadAll() {
      const r = await fetch('/app/projects');
      if (r.status === 401) {
        location.href = '/login';
        return;
      }
      if (!r.ok) {
        const d = await r.json().catch(() => ({ detail: 'error ' + r.status }));
        setFeedback(d.detail || ('error ' + r.status), true);
        return;
      }
      applyPayload(await r.json());
    }

    async function activateProject(slug) {
      setFeedback('', false);
      const r = await fetch('/app/projects/' + encodeURIComponent(slug) + '/activate', {
        method: 'POST',
      });
      const d = await r.json().catch(() => ({ detail: 'error ' + r.status }));
      if (!r.ok) {
        setFeedback(d.detail || ('error ' + r.status), true);
        return;
      }
      confirmingSlug = null;
      applyPayload(d);
      setFeedback('Active project switched to ' + slug + '.', false);
    }

    async function deleteProject(slug) {
      setFeedback('', false);
      const cb = document.getElementById('delFiles_' + slug);
      const delFiles = cb && cb.checked ? 'true' : 'false';
      const r = await fetch(
        '/app/projects/' + encodeURIComponent(slug) + '?delete_files=' + delFiles,
        { method: 'DELETE' }
      );
      const d = await r.json().catch(() => ({ detail: 'error ' + r.status }));
      if (!r.ok) {
        setFeedback(d.detail || ('error ' + r.status), true);
        return;
      }
      confirmingSlug = null;
      applyPayload(d);
      const extra = d.files_deletion_queued
        ? ' (workstation folder removal queued).'
        : '.';
      setFeedback('Removed project ' + slug + extra, false);
    }

    document.getElementById('searchInput').addEventListener('input', renderProjects);
    document.getElementById('pillAll').addEventListener('click', () => {
      watchOnly = false;
      renderProjects();
    });
    document.getElementById('pillWatch').addEventListener('click', () => {
      watchOnly = true;
      renderProjects();
    });

    document.getElementById('projectList').addEventListener('click', e => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;
      const slug = btn.dataset.slug;
      if (act === 'activate' && slug) {
        activateProject(slug);
      } else if (act === 'ask-delete' && slug) {
        confirmingSlug = confirmingSlug === slug ? null : slug;
        renderProjects();
      } else if (act === 'cancel-delete') {
        confirmingSlug = null;
        renderProjects();
      } else if (act === 'confirm-delete' && slug) {
        deleteProject(slug);
      }
    });

    loadAll();
  </script>
</body>
</html>
"""

DASHBOARD_PROJECTS_JS = """
function projectsCard(d) {
  if (!d.is_dev) return '';
  const info = d.dev_projects || {};
  const active = info.active_project || 'watch-llm-bridge';
  const count = info.count || 1;
  return `<p class="sub">Active workspace: <code>~/Code/${esc(active)}</code> · ${count} project${count === 1 ? '' : 's'} tracked.</p>
    <p class="sub">Search projects by name or summary, switch the active Carbon target, or clean up old projects.</p>
    <p><a class="btn" href="/projects">Open Carbon Projects</a></p>`;
}
"""
