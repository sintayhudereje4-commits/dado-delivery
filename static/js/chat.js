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
    const LAST_MSG_KEY = 'dado_chat_last_msg';
    let lastMsgId = parseInt(localStorage.getItem(LAST_MSG_KEY) || '0', 10) || 0;

    function persistLastMsgId() {
        try { localStorage.setItem(LAST_MSG_KEY, String(lastMsgId)); } catch (e) {}
    }

    function getSessionKey() {
        return localStorage.getItem(SESSION_KEY) || '';
    }

    function setSessionKey(key) {
        localStorage.setItem(SESSION_KEY, key);
    }

    function openChat() {
        widget.classList.add('is-open');
        fab.classList.add('open');
        if (badge) badge.hidden = true;
        loadMessages();
        startPolling();
        setTimeout(function () { input && input.focus(); }, 150);
    }

    function closeChat() {
        widget.classList.remove('is-open');
        fab.classList.remove('open');
        stopPolling();
    }

    function appendBubble(msg, scroll) {
        if (msg.id && msg.id <= lastMsgId && msg.sender !== 'system') return;
        if (msg.id) { lastMsgId = Math.max(lastMsgId, msg.id); persistLastMsgId(); }

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
        if (scroll !== false) {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    }

    function renderMessages(list) {
        // Keep the first system bubble if present
        const system = messagesEl.querySelector('.chat-bubble.system');
        messagesEl.innerHTML = '';
        if (system) messagesEl.appendChild(system);
        (list || []).forEach(function (m) {
            appendBubble(m, false);
        });
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function loadMessages() {
        const key = getSessionKey();
        if (!key) return;
        fetch('/api/chat/messages?session_key=' + encodeURIComponent(key))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.messages) renderMessages(data.messages);
            })
            .catch(function () {});
    }

    function pollMessages() {
        const key = getSessionKey();
        if (!key) return;
        fetch('/api/chat/messages?session_key=' + encodeURIComponent(key))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const list = data.messages || [];
                let newAdmin = false;
                list.forEach(function (m) {
                    if (m.id > lastMsgId) {
                        if (m.sender === 'admin') newAdmin = true;
                        appendBubble(m);
                    }
                });
                if (newAdmin && !widget.classList.contains('is-open') && badge) {
                    badge.hidden = false;
                }
            })
            .catch(function () {});
    }

    function startPolling(intervalMs) {
        stopPolling();
        pollTimer = setInterval(pollMessages, intervalMs || 4000);
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

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

    // Background poll for badge when closed — only if user has an active chat session
    setInterval(function () {
        if (widget.classList.contains('is-open')) return;
        if (!getSessionKey()) return;
        pollMessages();
    }, 15000);
})();
