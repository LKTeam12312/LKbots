const tg = window.Telegram?.WebApp;
const API_URL = '/api';
const state = { user: null, rating: 'locks', refreshTimer: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const loader = $('#loader');
const toast = $('#toast');

function haptic(type = 'light') {
    try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
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
        style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 2,
    }).format(Number(value) || 0);
}

function shortDate(value) {
    if (!value) return 'Дата не указана';
    if (typeof value === 'string' && /^\d{2}\.\d{2}\.\d{4}/.test(value)) return value;
    const normalized = typeof value === 'number' ? value * 1000 : String(value).replace(' ', 'T');
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}

function text(value, fallback = '—') {
    const result = String(value ?? '').trim();
    return result || fallback;
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

function renderUser(user) {
    state.user = user;
    const telegramUser = tg?.initDataUnsafe?.user || {};
    const displayName = text(user.name || telegramUser.first_name, 'Участник');
    const username = telegramUser.username ? `@${telegramUser.username}` : `ID ${user.user_id}`;

    $('#myBalance').textContent = money(user.cash);
    $('#lockCount').textContent = Number(user.locks) || 0;
    $('#profitCount').textContent = Number(user.profits) || 0;
    $('#earnedTotal').textContent = money(user.cash);
    $('#userInfo').textContent = text(user.tag, 'NOTSCAM');
    $('#rankLabel').textContent = text(user.rank, 'Студент');

    const locks = Array.isArray(user.lock_history) ? user.lock_history : [];
    $('#lockHistoryCount').textContent = locks.length;
    renderHistory($('#lockHistory'), locks, (item) => ({
        symbol: '⌁', title: text(item.model, 'Новый лок'), subtitle: shortDate(item.date || item.timestamp),
    }), 'Пока нет записей');

    const profits = Array.isArray(user.profit_history) ? user.profit_history : [];
    renderHistory($('#profitHistory'), profits, (item) => {
        const amount = typeof item === 'object' ? item.amount : item;
        return { symbol: '↗', title: 'Начисление', subtitle: shortDate(item?.date || item?.timestamp), value: `+${money(amount)}` };
    }, 'Пока нет начислений');

    ['1', '2', '3'].forEach((level) => {
        $(`#refCount${level}`).textContent = Number(user[`ref_count_l${level}`]) || 0;
        $(`#refMoney${level}`).textContent = (Number(user[`ref_money_l${level}`]) || 0).toFixed(2);
    });

    $('#profileName').textContent = displayName;
    $('#profileUsername').textContent = username;
    $('#profileRank').textContent = text(user.rank, 'Студент');
    $('#joinedAt').textContent = shortDate(user.joined_at);
    const initials = displayName.split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase();
    $('#profileAvatar').textContent = initials || 'LK';

    $('#tagInput').value = user.tag || '';
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

    users.forEach((user, index) => {
        const item = document.createElement('article');
        item.className = 'leader-item';
        const place = document.createElement('div');
        place.className = 'leader-place';
        place.textContent = index + 1;
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
        item.append(place, person, score);
        container.append(item);

        if (user.user_id === state.user?.user_id) $('#topPlace').textContent = `#${index + 1}`;
    });
}

async function loadLeaderboard(kind = state.rating) {
    state.rating = kind;
    $('#leaderboard').innerHTML = '<div class="empty-state">Загрузка рейтинга…</div>';
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
    haptic();
}

function closeSettings() {
    $('#settingsOverlay').classList.remove('show');
    $('#settingsOverlay').setAttribute('aria-hidden', 'true');
}

async function updateProfile(field, value, button) {
    button.disabled = true;
    try {
        const changed = await api('/me/update', { method: 'POST', body: JSON.stringify({ [field]: value }) });
        Object.assign(state.user, changed);
        renderUser(state.user);
        haptic('medium');
        showToast('Изменения сохранены');
    } catch (error) {
        showToast(error.message);
        try { tg?.HapticFeedback?.notificationOccurred('error'); } catch (_) {}
    } finally {
        button.disabled = false;
    }
}

function bindEvents() {
    $$('.bottom-nav button').forEach((button, index) => button.addEventListener('click', () => {
        const target = button.dataset.target;
        $$('.bottom-nav button').forEach((item) => item.classList.remove('active'));
        $$('.screen').forEach((screen) => screen.classList.remove('active'));
        button.classList.add('active');
        $(`[data-screen="${target}"]`).classList.add('active');
        $('.nav-indicator').style.transform = `translateX(${index * 100}%)`;
        if (target === 'rating') loadLeaderboard();
        haptic();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }));

    $$('.segment-control button').forEach((button) => button.addEventListener('click', () => {
        $$('.segment-control button').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        loadLeaderboard(button.dataset.rating);
        haptic();
    }));

    $('#settingsBtn').addEventListener('click', openSettings);
    $('#profileSettingsBtn').addEventListener('click', openSettings);
    $('#settingsCloseBtn').addEventListener('click', closeSettings);
    $('#settingsOverlay').addEventListener('click', (event) => { if (event.target === event.currentTarget) closeSettings(); });

    $('#tagSaveBtn').addEventListener('click', () => {
        const value = $('#tagInput').value.trim();
        if (!value) return showToast('Введите тег');
        updateProfile('tag', value, $('#tagSaveBtn'));
    });
    $('#rankSaveBtn').addEventListener('click', () => updateProfile('rank', $('#rankSelect').value, $('#rankSaveBtn')));

    $('#inviteBtn').addEventListener('click', async () => {
        const id = state.user?.user_id;
        if (!id) return;
        const link = `https://t.me/lk_team_bot?start=ref_${id}`;
        try {
            await navigator.clipboard.writeText(link);
            haptic('medium');
            showToast('Реферальная ссылка скопирована');
        } catch (_) {
            tg?.openTelegramLink?.(`https://t.me/share/url?url=${encodeURIComponent(link)}`);
        }
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
        if (!silent) setTimeout(() => showLoader(false), 240);
    }
}

async function init() {
    tg?.ready();
    tg?.expand();
    try {
        tg?.setBackgroundColor('#090a0d');
        tg?.setHeaderColor('#090a0d');
    } catch (_) {}
    bindEvents();
    await refresh();
    state.refreshTimer = setInterval(() => refresh(true), 60000);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') refresh(true);
    });
}

document.addEventListener('DOMContentLoaded', init);
