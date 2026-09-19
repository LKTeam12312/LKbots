const tg = window.Telegram?.WebApp;
const API_URL = '/api';
const state = {
    user: null,
    rating: 'locks',
    refreshTimer: null,
    screen: 'home',
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const loader = $('#loader');
const toast = $('#toast');

function haptic(type = 'light') {
    try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
}

function notifyHaptic(type = 'success') {
    try { tg?.HapticFeedback?.notificationOccurred(type); } catch (_) {}
}

function showLoader(show) {
    loader?.classList.toggle('active', Boolean(show));
}

let toastTimer;
function showToast(message) {
    if (!toast) return;
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('show');
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2800);
}

function money(value) {
    return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: 0,
        maximumFractionDigits: 2,
    }).format(Number(value) || 0);
}

function shortDate(value) {
    if (!value) return 'Дата не указана';
    if (typeof value === 'string' && /^\d{2}\.\d{2}\.\d{4}/.test(value)) return value;
    const normalized = typeof value === 'number' ? value * 1000 : String(value).replace(' ', 'T');
    const date = new Date(normalized);
    return Number.isNaN(date.getTime())
        ? String(value)
        : date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}

function text(value, fallback = '—') {
    const result = String(value ?? '').trim();
    return result || fallback;
}

function initials(value) {
    return text(value, 'LK')
        .split(/\s+/)
        .slice(0, 2)
        .map((part) => part[0])
        .join('')
        .toUpperCase();
}

function greeting() {
    const hour = new Date().getHours();
    if (hour < 6) return 'Доброй ночи';
    if (hour < 12) return 'Доброе утро';
    if (hour < 18) return 'Добрый день';
    return 'Добрый вечер';
}

async function api(path, options = {}) {
    if (!tg?.initData) throw new Error('Откройте Mini App через Telegram');
    const response = await fetch(`${API_URL}${path}`, {
        ...options,
        headers: {
            'Authorization': `tma ${tg.initData}`,
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.success) {
        throw new Error(payload.error || 'Сервис временно недоступен');
    }
    return payload.data;
}

function animateMoney(element, target) {
    if (!element) return;
    const next = Number(target) || 0;
    const previous = Number(element.dataset.value || 0);
    element.dataset.value = String(next);
    if (Math.abs(next - previous) < 0.01 || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        element.textContent = money(next);
        return;
    }
    const started = performance.now();
    const duration = 650;
    const frame = (now) => {
        const progress = Math.min((now - started) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        element.textContent = money(previous + (next - previous) * eased);
        if (progress < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
}

function makeHistoryItem({ symbol, title, subtitle, value }) {
    const item = document.createElement('article');
    item.className = 'history-item';

    const icon = document.createElement('div');
    icon.className = 'history-symbol';
    icon.textContent = symbol;

    const copy = document.createElement('div');
    copy.className = 'history-copy';
    const heading = document.createElement('strong');
    heading.textContent = title;
    const date = document.createElement('span');
    date.textContent = subtitle;
    copy.append(heading, date);

    item.append(icon, copy);
    if (value) {
        const amount = document.createElement('div');
        amount.className = 'history-value';
        amount.textContent = value;
        item.append(amount);
    }
    return item;
}

function renderHistory(container, items, mapper, emptyMessage) {
    if (!container) return;
    container.replaceChildren();
    if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = emptyMessage;
        container.append(empty);
        return;
    }
    items.slice().reverse().slice(0, 12).forEach((item) => container.append(makeHistoryItem(mapper(item))));
}

function renderProfitFacts(profits) {
    const amounts = profits
        .map((item) => Number(typeof item === 'object' ? item.amount : item) || 0)
        .filter((amount) => amount > 0);
    const total = amounts.reduce((sum, amount) => sum + amount, 0);
    const average = amounts.length ? total / amounts.length : 0;
    const best = amounts.length ? Math.max(...amounts) : 0;
    $('#profitAverage').textContent = money(average);
    $('#profitBest').textContent = money(best);
    $('#profitOperations').textContent = String(amounts.length);
}

function renderUser(user) {
    state.user = user;
    const telegramUser = tg?.initDataUnsafe?.user || {};
    const displayName = text(user.name || telegramUser.first_name, 'Участник');
    const username = telegramUser.username ? `@${telegramUser.username}` : `ID ${user.user_id}`;
    const userTag = text(user.tag, 'NOTSCAM');

    $('#greetingText').textContent = greeting();
    $('#welcomeName').textContent = displayName;
    animateMoney($('#myBalance'), user.cash);
    animateMoney($('#earnedTotal'), user.cash);
    $('#lockCount').textContent = Number(user.locks) || 0;
    $('#profitCount').textContent = Number(user.profits) || 0;
    $('#userInfo').textContent = userTag;
    $('#rankLabel').textContent = text(user.rank, 'Студент');

    const locks = Array.isArray(user.lock_history) ? user.lock_history : [];
    $('#lockHistoryCount').textContent = locks.length;
    renderHistory($('#lockHistory'), locks, (item) => ({
        symbol: 'L',
        title: text(item.model, 'Новый лок'),
        subtitle: shortDate(item.date || item.timestamp),
    }), 'Здесь появятся ваши последние локи');

    const profits = Array.isArray(user.profit_history) ? user.profit_history : [];
    renderProfitFacts(profits);
    renderHistory($('#profitHistory'), profits, (item) => {
        const amount = typeof item === 'object' ? item.amount : item;
        return {
            symbol: '↗',
            title: text(item?.deal_type === '+' ? 'Прямой профит' : 'Начисление'),
            subtitle: shortDate(item?.date || item?.timestamp),
            value: `+${money(amount)}`,
        };
    }, 'Здесь появится история начислений');

    ['1', '2', '3'].forEach((level) => {
        $(`#refCount${level}`).textContent = Number(user[`ref_count_l${level}`]) || 0;
        $(`#refMoney${level}`).textContent = (Number(user[`ref_money_l${level}`]) || 0).toFixed(2);
    });

    $('#profileName').textContent = displayName;
    $('#profileUsername').textContent = username;
    $('#profileRank').textContent = text(user.rank, 'Студент');
    $('#profileTag').textContent = userTag;
    $('#joinedAt').textContent = shortDate(user.joined_at);
    $('#profileAvatar').textContent = initials(displayName);
    $('#profileProfitCount').textContent = Number(user.profits) || 0;
    $('#profileLockCount').textContent = Number(user.locks) || 0;
    $('#profileMemberId').textContent = String(user.user_id || '').slice(-6).padStart(6, '0');

    $('#tagInput').value = user.tag || '';
    $('#tagCount').textContent = String((user.tag || '').length);
    $('#rankSelect').value = user.rank || 'Студент';
    updateRankOptions();
}

function updateRankOptions() {
    const balance = Number(state.user?.cash) || 0;
    $$('#rankSelect option').forEach((option) => {
        const locked = balance < Number(option.dataset.price || 0);
        option.disabled = locked;
        option.textContent = option.textContent.replace(/ · закрыт$/, '') + (locked ? ' · закрыт' : '');
    });
    $('#rankHint').textContent = `Текущий баланс: ${money(balance)}. Новые ранги открываются автоматически по балансу.`;
}

function renderLeaderboard(users, kind) {
    const container = $('#leaderboard');
    container.replaceChildren();
    if (!users.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = 'В рейтинге пока нет участников';
        container.append(empty);
        return;
    }

    let currentUserFound = false;
    users.forEach((user, index) => {
        const item = document.createElement('article');
        item.className = 'leader-item';
        if (index < 3) item.classList.add('is-top');
        if (user.user_id === state.user?.user_id) {
            item.classList.add('is-me');
            currentUserFound = true;
            $('#topPlace').textContent = `#${index + 1}`;
        }

        const place = document.createElement('div');
        place.className = 'leader-place';
        place.textContent = String(index + 1).padStart(2, '0');

        const avatar = document.createElement('div');
        avatar.className = 'leader-avatar';
        avatar.textContent = initials(user.name);

        const person = document.createElement('div');
        person.className = 'leader-person';
        const name = document.createElement('strong');
        name.textContent = text(user.name, 'Участник');
        const tag = document.createElement('span');
        tag.textContent = text(user.tag, user.rank || 'LK TEAM');
        person.append(name, tag);

        const score = document.createElement('div');
        score.className = 'leader-value';
        score.textContent = kind === 'balance' ? money(user.cash) : String(Number(user[kind]) || 0);
        item.append(place, avatar, person, score);
        container.append(item);
    });

    if (!currentUserFound && kind === state.rating) $('#topPlace').textContent = '10+';
}

async function loadLeaderboard(kind = state.rating) {
    state.rating = kind;
    $('#leaderboard').innerHTML = '<div class="empty-state">Формируем рейтинг…</div>';
    try {
        const users = await api(`/leaderboard/${kind}`);
        renderLeaderboard(users, kind);
    } catch (error) {
        $('#leaderboard').innerHTML = '<div class="empty-state">Не удалось загрузить рейтинг</div>';
        showToast(error.message);
    }
}

function openSettings() {
    $('#settingsOverlay').classList.add('show');
    $('#settingsOverlay').setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    haptic();
}

function closeSettings() {
    $('#settingsOverlay').classList.remove('show');
    $('#settingsOverlay').setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
}

async function updateProfile(field, value, button) {
    button.disabled = true;
    try {
        const changed = await api('/me/update', {
            method: 'POST',
            body: JSON.stringify({ [field]: value }),
        });
        Object.assign(state.user, changed);
        renderUser(state.user);
        notifyHaptic('success');
        showToast('Изменения сохранены');
    } catch (error) {
        showToast(error.message);
        notifyHaptic('error');
    } finally {
        button.disabled = false;
    }
}

function switchScreen(target, button, index) {
    if (state.screen === target) return;
    state.screen = target;
    $$('.bottom-nav button').forEach((item) => item.classList.remove('active'));
    $$('.screen').forEach((screen) => screen.classList.remove('active'));
    button.classList.add('active');
    $(`[data-screen="${target}"]`).classList.add('active');
    $('.nav-indicator').style.transform = `translateX(${index * 100}%)`;
    if (target === 'rating') loadLeaderboard();
    haptic();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function bindEvents() {
    $$('.bottom-nav button').forEach((button, index) => {
        button.addEventListener('click', () => switchScreen(button.dataset.target, button, index));
    });

    $$('.segment-control button').forEach((button) => button.addEventListener('click', () => {
        $$('.segment-control button').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        loadLeaderboard(button.dataset.rating);
        haptic();
    }));

    $('#settingsBtn').addEventListener('click', openSettings);
    $('#profileSettingsBtn').addEventListener('click', openSettings);
    $('#settingsCloseBtn').addEventListener('click', closeSettings);
    $('#settingsOverlay').addEventListener('click', (event) => {
        if (event.target === event.currentTarget) closeSettings();
    });

    $('#tagInput').addEventListener('input', (event) => {
        $('#tagCount').textContent = String(event.target.value.length);
    });
    $('#tagSaveBtn').addEventListener('click', () => {
        const value = $('#tagInput').value.trim();
        if (!value) return showToast('Введите тег');
        updateProfile('tag', value, $('#tagSaveBtn'));
    });
    $('#rankSaveBtn').addEventListener('click', () => {
        updateProfile('rank', $('#rankSelect').value, $('#rankSaveBtn'));
    });

    $('#inviteBtn').addEventListener('click', async () => {
        const id = state.user?.user_id;
        if (!id) return;
        const link = `https://t.me/lk_team_bot?start=ref_${id}`;
        try {
            await navigator.clipboard.writeText(link);
            haptic('medium');
            showToast('Приглашение скопировано');
        } catch (_) {
            tg?.openTelegramLink?.(`https://t.me/share/url?url=${encodeURIComponent(link)}`);
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeSettings();
    });
}

async function refresh(silent = false) {
    if (!silent) showLoader(true);
    try {
        const user = await api('/me');
        renderUser(user);
    } catch (error) {
        showToast(error.message);
        $('#userInfo').textContent = 'Нет подключения';
    } finally {
        if (!silent) setTimeout(() => showLoader(false), 420);
    }
}

async function init() {
    tg?.ready();
    tg?.expand();
    try {
        tg?.setBackgroundColor('#090806');
        tg?.setHeaderColor('#090806');
        tg?.enableClosingConfirmation?.();
    } catch (_) {}

    bindEvents();
    await refresh();
    if (state.user) loadLeaderboard('locks');
    state.refreshTimer = setInterval(() => refresh(true), 60000);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') refresh(true);
    });
}

document.addEventListener('DOMContentLoaded', init);
