const $ = s => document.querySelector(s), esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])), pretty = v => { try { return JSON.stringify(typeof v === 'string' ? JSON.parse(v) : v, null, 2) } catch { return String(v ?? '') } }, dt = s => s ? new Date(s).toLocaleString() : ''; let timer = null, busy = false;
function clearView(m = 'Selezionare una sessione.') { $('#meta').hidden = true; $('#chat').innerHTML = `<div class="empty">${esc(m)}</div>`; $('#memory').hidden = true }

function splitCachedContext(text) {
    const s = text.trim().replace(/^\[/, '').replace(/\]$/, '');

    const items = [];
    let current = '';
    let quote = null;
    let escaped = false;
    let depth = 0;

    for (let i = 0; i < s.length; i++) {
        const c = s[i];

        if (escaped) {
            current += c;
            escaped = false;
            continue;
        }

        if (c === '\\') {
            current += c;
            escaped = true;
            continue;
        }

        if (quote) {
            current += c;
            if (c === quote) quote = null;
            continue;
        }

        if (c === "'" || c === '"') {
            quote = c;
            current += c;
            continue;
        }

        if (c === '{' || c === '[' || c === '(') {
            depth++;
        } else if (c === '}' || c === ']' || c === ')') {
            depth--;
        }

        // Divide solo sulle virgole della lista principale
        if (c === ',' && depth === 0) {
            if (current.trim()) items.push(current.trim());
            current = '';
            continue;
        }

        current += c;
    }

    if (current.trim()) items.push(current.trim());

    return items;
}


function renderUserContent(content) {
    const text = String(content ?? '');

    const contextMatch = text.match(
        /<cached_context>\s*([\s\S]*?)\s*<\/cached_context>/i
    );

    const requestMatch = text.match(
        /<user_request>\s*([\s\S]*?)\s*<\/user_request>/i
    );

    // Messaggio user normale
    if (!contextMatch && !requestMatch) {
        return `<div class="content">${esc(text)}</div>`;
    }

    const context = contextMatch?.[1]?.trim() ?? '';
    const request = requestMatch?.[1]?.trim() ?? '';

    let html = '';

    if (request) {
        html += `<div class="content">${esc(request)}</div>`;
    }

    if (context) {
        const items = splitCachedContext(context);

        html += `
            <details class="cachedContext">
                <summary>Cached context (${items.length})</summary>
                <div class="cachedContextBody">
                    ${items.map(item =>
                        `<div class="cachedContextItem">${esc(item)}</div>`
                    ).join('')}
                </div>
            </details>
        `;
    }

    return html;
}

function render(data, file) {
    const h = Array.isArray(data.history) ? data.history : [];

    $('#meta').hidden = false;
    $('#meta').innerHTML = `
        <div>
            <div class="label">Sessione</div>
            ${esc(data.id || file.name)}
        </div>
        <div>
            <div class="label">Creata</div>
            ${esc(dt(data.created_at))}
        </div>
        <div>
            <div class="label">File</div>
            ${esc(file.name)}
        </div>
        <div>
            <div class="label">Aggiornato</div>
            ${esc(dt(file.modified_at))}
        </div>
    `;

    const c = $('#chat');
    c.innerHTML = '';

    for (const x of h) {
        const r = document.createElement('div');

        r.className = `row ${x.role || ''}`;

        const t = x.role === 'tool';

        r.innerHTML = `
            <article class="bubble">
                <div>
                    <span class="role">
                        ${t ? 'SERVICE' : esc((x.role || '').toUpperCase())}
                    </span>

                    ${
                        t
                            ? `<span class="badge">${esc(x.name || '')}</span>`
                            : ''
                    }
                </div>

                ${
                    t && x.args
                        ? `<pre>${esc(pretty(x.args))}</pre>`
                        : ''
                }

                ${x.role === 'user'
                    ? renderUserContent(x.content)
                    : `<div class="content">${esc(t ? pretty(x.content) : x.content)}</div>`
                }

                ${
                    x.thinking
                        ? `
                            <details>
                                <summary>Thinking</summary>
                                <div class="content">
                                    ${esc(x.thinking)}
                                </div>
                            </details>
                        `
                        : ''
                }

                ${
                    x.mismatched_tools?.length
                        ? `
                            <details>
                                <summary>
                                    Mismatched tools (${x.mismatched_tools.length})
                                </summary>
                                <pre>${esc(pretty(x.mismatched_tools))}</pre>
                            </details>
                        `
                        : ''
                }

                <div class="time">
                    ${esc(dt(x.created_at))}
                </div>
            </article>
        `;

        c.appendChild(r);
    }

    if (!h.length) {
        c.innerHTML = '<div class="empty">Il log non contiene eventi.</div>';
    }

    const m = data.memory || {};

    $('#memory').hidden = false;
    $('#memoryBody').innerHTML = `
        <div>
            <span class="label">Task</span>
            <div>${esc(m.task || '-')}</div>
        </div>

        <div>
            <span class="label">Notes</span>

            ${
                m.notes?.length
                    ? `
                        <ul class="notes">
                            ${m.notes
                                .map(n => `<li><code>${esc(n)}</code></li>`)
                                .join('')}
                        </ul>
                    `
                    : '<div>-</div>'
            }
        </div>
    `;
}


async function api(u, o) {
    const r = await fetch(u, o);
    const j = await r.json();

    if (!r.ok) {
        throw new Error(
            j.error + (j.detail ? ': ' + j.detail : '')
        );
    }

    return j;
}


async function loadSessions(autoSelect = false) {
    try {
        const j = await api('/api/sessions?ts=' + Date.now());
        const s = $('#sessions');
        const old = s.value;

        s.innerHTML =
            '<option value="">Nessuna sessione</option>' +
            j.sessions
                .map(
                    x => `
                        <option value="${esc(x.name)}">
                            ${esc(x.name)} — ${esc(dt(x.modified_at))}
                        </option>
                    `
                )
                .join('');

        $('#dirPath').value = j.sessions_dir;

        const wanted =
            j.selected_log ||
            old ||
            (autoSelect && j.sessions[0]?.name) ||
            '';

        $('#currentSessionName').textContent =
            wanted || 'Nessuna sessione';

        if (
            wanted &&
            j.sessions.some(x => x.name === wanted)
        ) {
            s.value = wanted;

            if (!j.selected_log) {
                await selectSession(wanted);
            }
        }

        $('#status').textContent = j.warning
            ? 'Directory non disponibile: ' + j.warning
            : `${j.sessions.length} sessioni`;

    } catch (e) {
        $('#status').textContent = 'Errore: ' + e.message;
    }
}


async function selectSession(name = $('#sessions').value) {
    $('#currentSessionName').textContent =
        name || 'Nessuna sessione';

    if (!name) {
        clearView();
        return;
    }

    try {
        await api('/api/session/select', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ name })
        });

        await reload();

    } catch (e) {
        $('#status').textContent = 'Errore: ' + e.message;
    }
}


async function setDir() {
    const p = $('#dirPath').value.trim();

    if (!p) {
        return;
    }

    try {
        await api('/api/directory', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ path: p })
        });

        $('#currentSessionName').textContent = 'Nessuna sessione';

        clearView();

        await loadSessions(true);

    } catch (e) {
        $('#status').textContent = 'Errore: ' + e.message;
    }
}


async function reload() {
    if (busy) {
        return;
    }

    busy = true;

    try {
        const j = await api('/api/log?ts=' + Date.now());

        if (!j.data) {
            clearView(j.message);
            $('#status').textContent = j.message;
            return;
        }

        render(j.data, j.file);

        $('#status').textContent =
            'Aggiornato: ' + new Date().toLocaleTimeString();

    } catch (e) {
        $('#status').textContent = 'Errore: ' + e.message;

    } finally {
        busy = false;
    }
}


function resetTimer() {
    if (timer) {
        clearInterval(timer);
    }

    timer = null;

    if ($('#auto').checked) {
        timer = setInterval(
            async () => {
                await loadSessions(false);
                await reload();
            },
            Number($('#interval').value)
        );
    }
}


// Event handlers

$('#setDir').addEventListener(
    'click',
    setDir
);

$('#refreshSessions').addEventListener(
    'click',
    () => loadSessions(false)
);

$('#sessions').addEventListener(
    'change',
    () => selectSession()
);

$('#reload').addEventListener(
    'click',
    reload
);

$('#bottom').addEventListener(
    'click',
    () => window.scrollTo({
        top: document.documentElement.scrollHeight,
        behavior: 'smooth'
    })
);

$('#auto').addEventListener(
    'change',
    resetTimer
);

$('#interval').addEventListener(
    'change',
    resetTimer
);


// Initialization

(async () => {
    const s = await api('/api/status');

    $('#dirPath').value = s.sessions_dir;

    await loadSessions(true);
})();