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
