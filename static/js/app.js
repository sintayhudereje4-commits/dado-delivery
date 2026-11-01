/* Dado Delivery - Client-side cart & UI */

/* CSRF: send the token with every same-origin state-changing fetch() */
(function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var token = meta ? meta.getAttribute('content') : '';
    var origFetch = window.fetch;
    if (!token || !origFetch) return;
    window.fetch = function (input, init) {
        init = init || {};
        var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            var url = typeof input === 'string' ? input : ((input && input.url) || '');
            var sameOrigin = (url.charAt(0) === '/' && url.charAt(1) !== '/') || url.indexOf(location.origin) === 0;
            if (sameOrigin) {
                var headers = new Headers(init.headers || (typeof input !== 'string' && input && input.headers) || {});
                headers.set('X-CSRF-Token', token);
                init.headers = headers;
            }
        }
        return origFetch.call(this, input, init);
    };
})();

/* Escape text before putting it into innerHTML */
function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

const CART_KEY = 'dado_cart';

function getCart() {
    try {
        return JSON.parse(localStorage.getItem(CART_KEY)) || [];
    } catch {
        return [];
    }
}

function saveCart(cart) {
    localStorage.setItem(CART_KEY, JSON.stringify(cart));
    updateCartCount();
}

function clearCart() {
    localStorage.removeItem(CART_KEY);
    updateCartCount();
}

function addToCart(item) {
    const cart = getCart();
    const existing = cart.find(c => c.variant_id === item.variant_id);
    if (existing) {
        existing.quantity += item.quantity;
        // Re-apply bulk pricing if thresholds are known on the line
        if (existing.bulk_price != null && existing.bulk_min && existing.quantity >= existing.bulk_min) {
            existing.price = existing.bulk_price;
        } else if (existing.base_price != null) {
            existing.price = existing.base_price;
        }
    } else {
        cart.push(item);
    }
    saveCart(cart);
}

function updateCartCount() {
    const cart = getCart();
    const count = cart.reduce((sum, i) => sum + i.quantity, 0);
    const el = document.getElementById('cartCount');
    if (el) {
        el.textContent = count;
        el.classList.add('bump');
        setTimeout(() => el.classList.remove('bump'), 300);
    }
}

/* Soft toast notification (no browser alert) */
function showToast(message, type) {
    type = type || 'success';
    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    // trigger enter animation
    requestAnimationFrame(function () {
        toast.classList.add('show');
    });
    setTimeout(function () {
        toast.classList.remove('show');
        setTimeout(function () {
            toast.remove();
        }, 300);
    }, 2200);
}

// Mobile menu
document.addEventListener('DOMContentLoaded', function () {
    updateCartCount();

    const toggle = document.getElementById('menuToggle');
    const nav = document.getElementById('mainNav');
    if (toggle && nav) {
        toggle.addEventListener('click', function () {
            nav.classList.toggle('open');
        });
    }

    // Auto-hide flash messages
    document.querySelectorAll('.flash').forEach(function (f) {
        setTimeout(function () {
            f.style.opacity = '0';
            f.style.transition = 'opacity 0.4s';
            setTimeout(function () {
                f.remove();
            }, 400);
        }, 4000);
    });
});


/** Refresh cart prices from server (daily price updates). cb(cart, removedCount) */
function refreshCartPrices(cb) {
    var cart = getCart();
    if (!cart.length) { if (cb) cb(cart, 0); return; }
    fetch('/api/cart/prices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: cart.map(function(i) {
            return { variant_id: i.variant_id, quantity: i.quantity };
        })})
    }).then(function(r){ return r.json(); }).then(function(data) {
        // An error reply (expired session, rate limit, ...) must never wipe the cart.
        if (!data || !Array.isArray(data.items)) { if (cb) cb(cart, 0); return; }
        var map = {};
        data.items.forEach(function(x){ map[x.variant_id] = x; });
        var next = [];
        var removed = 0;
        cart.forEach(function(item) {
            var s = map[item.variant_id];
            if (!s) { next.push(item); return; }          // unknown to the server reply: keep as is
            if (s.available === false) { removed++; return; }
            item.price = s.price;
            item.base_price = s.base_price;
            item.bulk_price = s.bulk_price;
            item.bulk_min = s.bulk_min;
            if (s.name) item.name = s.name;
            if (s.weight_kg != null) item.weight_kg = s.weight_kg;
            if (s.unit) {
                item.unit = s.unit;
                var _ul = {kg:'ኪ.ግ', L:'ሊትር', pcs:'ብዛት', pack:'ጥቅል', bag:'ከረጢት'};
                item.unit_label = _ul[s.unit] || s.unit;
            }
            if (typeof s.stock === 'number') {
                item.stock = s.stock;
                if (s.stock <= 0) {
                    removed++;
                    return; // drop out-of-stock lines from cart
                }
                if (item.quantity > s.stock) {
                    item.quantity = s.stock;
                }
            }
            next.push(item);
        });
        saveCart(next);
        if (removed > 0) {
            var am = (document.documentElement.lang || '').indexOf('am') === 0;
            showToast(am ? 'አንዳንድ እቃዎች አሁን አይገኙም፤ ከጋሪው ተወግደዋል' : 'Some items are no longer available and were removed from your cart', 'error');
        }
        if (cb) cb(next, removed);
    }).catch(function(){ if (cb) cb(cart, 0); });
}


/* If the browser was refreshed / closed while an order was being sent, find out whether the
   server created it. If yes: empty the cart and go to the order page (no duplicate, no confusion). */
(function recoverCheckout() {
    var KEY = 'dado_checkout_pending';
    var pend = null;
    try { pend = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
    if (!pend || !pend.key) { try { localStorage.removeItem(KEY); } catch (e) {} return; }
    var tries = 0;
    function forget() { try { localStorage.removeItem(KEY); } catch (e) {} }
    function check() {
        fetch('/api/checkout/recover?key=' + encodeURIComponent(pend.key) + '&_=' + Date.now(),
              { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.found) {
                    forget();
                    clearCart();
                    var p = location.pathname;
                    if (p.indexOf('/cart') === 0 || p.indexOf('/checkout') === 0) {
                        location.replace(d.redirect);
                    } else if (typeof showToast === 'function') {
                        showToast('Your order was received — open Cart to see it.', 'success');
                    }
                    return;
                }
                tries++;
                // The request may still be on its way: look again for ~90 seconds, then give up.
                if (Date.now() - (pend.ts || 0) < 90000 && tries < 8) setTimeout(check, 3000);
                else forget();
            })
            .catch(function () { if (++tries < 4) setTimeout(check, 4000); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', check);
    else check();
})();

/* Product search: suggestions while typing; Enter / the button open the full result list. */
(function productSearch() {
    function init() {
        var input = document.getElementById('siteSearch');
        var box = document.getElementById('searchSuggest');
        if (!input || !box) return;
        var am = (document.documentElement.lang || '').toLowerCase().indexOf('am') === 0;
        var timer = null, seq = 0, items = [], active = -1;

        function close() { box.hidden = true; box.innerHTML = ''; active = -1; }

        function render(list, term) {
            items = list;
            box.innerHTML = '';
            if (!list.length) {
                var none = document.createElement('div');
                none.className = 'ss-none';
                none.textContent = am ? 'ምንም ምርት አልተገኘም' : 'No product found';
                box.appendChild(none);
                box.hidden = false;
                return;
            }
            list.forEach(function (it) {
                var a = document.createElement('a');
                a.href = it.url;
                var pic;
                if (it.image) { pic = document.createElement('img'); pic.src = it.image; pic.alt = ''; pic.loading = 'lazy'; }
                else { pic = document.createElement('span'); pic.className = 'ss-ph'; pic.textContent = (it.name || '?').charAt(0); }
                var nm = document.createElement('span'); nm.className = 'ss-name'; nm.textContent = it.name;
                var pr = document.createElement('span'); pr.className = 'ss-price';
                pr.textContent = it.in_stock && it.price ? (am ? 'ከ ' : 'From ') + Math.round(it.price) + ' ብር' : (am ? 'አሁን አይገኝም' : 'Out of stock');
                a.appendChild(pic); a.appendChild(nm); a.appendChild(pr);
                box.appendChild(a);
            });
            var all = document.createElement('a');
            all.className = 'ss-all';
            all.href = '/?q=' + encodeURIComponent(term);
            all.textContent = am ? 'ሁሉንም ውጤት ይመልከቱ ›' : 'See all results ›';
            box.appendChild(all);
            box.hidden = false;
        }

        function search() {
            var term = input.value.trim();
            if (term.length < 2) { close(); return; }
            var mine = ++seq;
            fetch('/api/search?q=' + encodeURIComponent(term), { credentials: 'same-origin', cache: 'no-store' })
                .then(function (r) { return r.json(); })
                .then(function (d) { if (mine === seq) render(d.items || [], term); })
                .catch(function () {});
        }

        input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(search, 250); });
        input.addEventListener('focus', function () { if (input.value.trim().length >= 2) search(); });
        input.addEventListener('keydown', function (e) {
            var links = box.querySelectorAll('a');
            if (e.key === 'Escape') { close(); return; }
            if (!links.length || box.hidden) return;
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                active = (active + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
                links.forEach(function (l, i) { l.classList.toggle('is-active', i === active); });
            } else if (e.key === 'Enter' && active >= 0) {
                e.preventDefault();
                window.location.href = links[active].href;
            }
        });
        document.addEventListener('click', function (e) { if (!box.contains(e.target) && e.target !== input) close(); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
