/* Dado Delivery - Live Help Chat Widget */

(function () {
    const SESSION_KEY = 'dado_chat_session';
    const fab = document.getElementById('helpFab');
    const widget = document.getElementById('chatWidget');
    const closeBtn = document.getElementById('chatClose');
    const form = document.getElementById('chatForm');
    const input = document.getElementById('chatInput');
    const messagesEl = document.getElementById('chatMessages');
    const badge = document.getElementById('chatBadge');
    const nameInput = document.getElementById('chatName');
    const phoneInput = document.getElementById('chatPhone');

    if (!fab || !widget) return;

    let pollTimer = null;
    const SEEN_KEY = 'dado_chat_seen_upto';
    // Ids already drawn in the open window. We compare by id (not "bigger than the last one"),
    // so a reset database or an old stored number can never hide a new admin reply.
    const drawn = new Set();
    let seenUpTo = parseInt(localStorage.getItem(SEEN_KEY) || '0', 10) || 0;
    let busy = false;

    function persistSeen() {
        try { localStorage.setItem(SEEN_KEY, String(seenUpTo)); } catch (e) {}
    }

    function getSessionKey() {
        return localStorage.getItem(SESSION_KEY) || '';
    }

    function setSessionKey(key) {
        localStorage.setItem(SESSION_KEY, key);
    }

    function isOpen() {
        return widget.classList.contains('is-open');
    }

    function openChat() {
        widget.classList.add('is-open');
        fab.classList.add('open');
        if (badge) badge.hidden = true;
        fetchMessages(true);
        startPolling();
        setTimeout(function () { input && input.focus(); }, 150);
    }

    function closeChat() {
        widget.classList.remove('is-open');
        fab.classList.remove('open');
        startPolling(); // keep a slower poll so the badge still works
    }

    function appendBubble(msg) {
        if (msg.id && drawn.has(msg.id)) return false;
        if (msg.id) drawn.add(msg.id);
        const div = document.createElement('div');
        div.className = 'chat-bubble ' + (msg.sender || 'system');
        if (msg.sender === 'ai') {
            const lab = document.createElement('div');
            lab.className = 'chat-ai-label';
            lab.textContent = '💬 Help';
            div.appendChild(lab);
        }
        const body = document.createElement('div');
        body.className = 'chat-text';
        body.style.whiteSpace = 'pre-wrap';
        body.textContent = msg.message;
        div.appendChild(body);
        if (msg.created_at) {
            const time = document.createElement('span');
            time.className = 'chat-time';
            time.textContent = msg.created_at;
            div.appendChild(time);
        }
        messagesEl.appendChild(div);
        return true;
    }

    function scrollDown() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function fetchMessages(fullRedraw) {
        const key = getSessionKey();
        if (!key || busy) return;
        busy = true;
        // timestamp in the URL + no-store: the answer can never come from a cache
        fetch('/api/chat/messages?session_key=' + encodeURIComponent(key) + '&_=' + Date.now(), { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                busy = false;
                const list = data.messages || [];
                const serverMax = data.max_id || 0;
                if (seenUpTo > serverMax) { seenUpTo = 0; persistSeen(); } // server was reset
                if (fullRedraw) {
                    const system = messagesEl.querySelector('.chat-bubble.system');
                    messagesEl.innerHTML = '';
                    if (system) messagesEl.appendChild(system);
                    drawn.clear();
                }
                let added = false, unreadFromTeam = false;
                list.forEach(function (m) {
                    if (appendBubble(m)) added = true;
                    if (m.sender !== 'visitor' && m.id > seenUpTo) unreadFromTeam = true;
                });
                if (isOpen()) {
                    if (added) scrollDown();
                    if (serverMax > seenUpTo) { seenUpTo = serverMax; persistSeen(); }
                } else if (unreadFromTeam && badge) {
                    badge.hidden = false;
                }
            })
            .catch(function () { busy = false; });
    }

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(function () {
            if (document.visibilityState !== 'visible') return;
            fetchMessages(false);
        }, isOpen() ? 4000 : 20000);
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    // Coming back to the tab: check immediately instead of waiting for the next tick.
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && getSessionKey()) fetchMessages(false);
    });

    fab.addEventListener('click', function (e) {
        e.stopPropagation();
        if (widget.classList.contains('is-open')) closeChat();
        else openChat();
    });

    // Click outside closes chat
    document.addEventListener('click', function (e) {
        if (!widget.classList.contains('is-open')) return;
        if (widget.contains(e.target) || fab.contains(e.target)) return;
        closeChat();
    });

    if (closeBtn) {
        closeBtn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            closeChat();
        });
    }
    widget.addEventListener('click', function (e) {
        e.stopPropagation();
    });

    if (form) {
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            const text = (input.value || '').trim();
            if (!text) return;
            const payload = {
                session_key: getSessionKey(),
                message: text,
                name: (nameInput && nameInput.value) || '',
                phone: (phoneInput && phoneInput.value) || ''
            };
            input.value = '';
            input.disabled = true;
            fetch('/api/chat/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    input.disabled = false;
                    input.focus();
                    if (data.success) {
                        if (data.session_key) setSessionKey(data.session_key);
                        if (data.message) appendBubble(data.message);
                        if (data.ai_message) appendBubble(data.ai_message);
                        scrollDown();
                        fetchMessages(false);
                    } else {
                        // Keep the visitor's text so nothing is lost, and tell them why it failed
                        input.value = text;
                        if (typeof showToast === 'function') {
                            showToast(data.error || 'Message could not be sent', 'error');
                        }
                    }
                })
                .catch(function () {
                    input.disabled = false;
                    if (typeof showToast === 'function') {
                        showToast('Network error', 'error');
                    }
                });
        });
    }

    // Start in the background (badge for new replies while the window is closed)
    if (getSessionKey()) { startPolling(); fetchMessages(false); }
})();
