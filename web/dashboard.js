(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    user: null, csrf: null, guilds: [], guildId: null, snapshot: null,
    statistics: [], statisticsTotal: 0, statisticsLoading: false, statisticsError: null,
    activeView: 'server', sectionId: null, baseline: new Map(), drafts: new Map(), busy: false,
    loading: false, alert: null, loadError: null, requestId: 0
  };
  const svgNS = 'http://www.w3.org/2000/svg';
  const paths = {
    settings: ['M4 7h16M4 17h16M8 4v6M16 14v6'],
    refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M5.6 9a7 7 0 0 1 12-2L20 12M4 12l2.4 5a7 7 0 0 0 12-2'],
    arrow: ['M5 12h14M14 7l5 5-5 5']
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.6');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    (paths[name] || paths.settings).forEach((d) => {
      const path = document.createElementNS(svgNS, 'path');
      path.setAttribute('d', d);
      svg.append(path);
    });
    return svg;
  }
  function button(text, className, handler) {
    const node = el('button', className, text);
    node.type = 'button';
    if (handler) node.addEventListener('click', handler);
    return node;
  }
  function announce(message) { $('live-region').textContent = message; }
  function setAvatar(node, name, url) {
    node.replaceChildren();
    const initial = (String(name || '?').trim().charAt(0) || '?').toUpperCase();
    node.textContent = initial;
    if (!url || typeof url !== 'string') return;
    try {
      const parsed = new URL(url, location.origin);
      if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return;
      const image = document.createElement('img');
      image.alt = '';
      image.referrerPolicy = 'no-referrer';
      image.src = parsed.href;
      image.addEventListener('error', () => { if (image.parentNode === node) node.textContent = initial; });
      node.replaceChildren(image);
    } catch (_) { /* Keep the initial when an image URL is invalid. */ }
  }
  function copy(value) {
    return value && typeof value === 'object' ? JSON.parse(JSON.stringify(value)) : value;
  }
  function readonly(field) { return Boolean(field.read_only || field.disabled); }
  function fields() {
    return state.snapshot && Array.isArray(state.snapshot.sections)
      ? state.snapshot.sections.flatMap((section) => Array.isArray(section.fields) ? section.fields : []) : [];
  }
  function equalValues(a, b, type) {
    if (type === 'rules_editor') return JSON.stringify(a) === JSON.stringify(b);
    if (type === 'multi_select' && Array.isArray(a) && Array.isArray(b)) {
      return a.length === b.length && a.slice().sort().every((value, i) => value === b.slice().sort()[i]);
    }
    if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && JSON.stringify(a) === JSON.stringify(b);
    return a === b;
  }
  function hasDraftChange(field) {
    return state.baseline.has(field.key) && state.drafts.has(field.key) &&
      !equalValues(state.baseline.get(field.key), state.drafts.get(field.key), field.type);
  }
  function isDirty(field) { return !readonly(field) && hasDraftChange(field); }
  function sectionDirty(section) { return (section.fields || []).some(hasDraftChange); }
  function hasChanges() { return fields().some(hasDraftChange); }
  function currentSection() {
    return state.snapshot && state.snapshot.sections.find((section) => String(section.id) === state.sectionId);
  }
  function setAlert(kind, message) {
    state.alert = { kind, message: String(message) };
    announce(message);
  }
  function errorMessage(error) {
    if (error && error.status === 401) return 'Your session has expired. Sign in again to continue.';
    if (error && error.status === 403) return 'You don’t have permission to do that, or your access has changed.';
    return (error && error.message) || 'Something went wrong. Please try again.';
  }
  async function request(url, options = {}) {
    let response;
    try {
      response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options });
    } catch (_) {
      throw new Error('Could not connect to the dashboard. Check your connection and try again.');
    }
    if (!response.ok) {
      let detail = '';
      try {
        const body = await response.json();
        if (typeof body.error === 'string') detail = body.error;
        else if (typeof body.detail === 'string') detail = body.detail;
        else if (typeof body.message === 'string') detail = body.message;
      } catch (_) { /* A response need not contain JSON. */ }
      const error = new Error(detail || `The server returned an error (${response.status}). Please try again.`);
      error.status = response.status;
      throw error;
    }
    if (options.expectNoBody) return null;
    try { return await response.json(); }
    catch (_) {
      if (options.optionalJson) return null;
      throw new Error('The server returned an unexpected response. Please try again.');
    }
  }
  function validSnapshot(snapshot) {
    return snapshot && snapshot.guild && Array.isArray(snapshot.sections) &&
      snapshot.sections.every((section) => section && Array.isArray(section.fields));
  }
  function applySnapshot(snapshot, preserve = new Set()) {
    if (!validSnapshot(snapshot)) throw new Error('The server returned an incomplete settings snapshot. Please try again.');
    const oldDrafts = state.drafts;
    const oldBaseline = state.baseline;
    state.snapshot = snapshot;
    state.baseline = new Map();
    state.drafts = new Map();
    fields().forEach((field) => {
      if (typeof field.key !== 'string') return;
      const keepDraft = preserve.has(field.key) && oldDrafts.has(field.key) && oldBaseline.has(field.key);
      state.baseline.set(field.key, copy(keepDraft ? oldBaseline.get(field.key) : field.value));
      state.drafts.set(field.key, copy(keepDraft ? oldDrafts.get(field.key) : field.value));
    });
    if (state.sectionId && !currentSection()) state.sectionId = null;
  }

  async function start() {
    $('initial-state').hidden = false;
    $('initial-state').replaceChildren(el('span', 'spinner'), el('span', '', 'opening your dashboard...'));
    try {
      const me = await request('/dashboard/api/me');
      if (!me || typeof me.authenticated !== 'boolean') throw new Error('The server returned an unexpected session response. Please try again.');
      $('initial-state').hidden = true;
      if (!me.authenticated) {
        showLogin(Boolean(me.oauth_configured));
        loadLoginBackdrop();
        return;
      }
      state.user = me.user || {};
      state.csrf = typeof me.csrf === 'string' && me.csrf ? me.csrf : null;
      $('account-name').textContent = state.user.username || 'Discord user';
      setAvatar($('account-avatar'), state.user.username, state.user.avatar_url);
      $('account-area').hidden = false;
      $('app-shell').hidden = false;
      $('logout-button').disabled = !state.csrf;
      await Promise.all([loadGuilds(), loadStatistics()]);
    } catch (error) {
      const area = $('initial-state');
      area.hidden = false;
      area.replaceChildren();
      const box = el('div', 'initial-error');
      box.append(el('p', '', errorMessage(error)), button('try again', 'button', start));
      if (error && error.status === 401) box.append(loginLink());
      area.append(box);
    }
  }
  function showLogin(oauthConfigured) {
    $('login-page').hidden = false;
    const login = $('login-button');
    $('login-unavailable').hidden = oauthConfigured;
    if (!oauthConfigured) {
      login.removeAttribute('href');
      login.setAttribute('aria-disabled', 'true');
      login.tabIndex = -1;
    } else {
      login.href = '/dashboard/login';
      login.removeAttribute('aria-disabled');
      login.removeAttribute('tabindex');
    }
  }
  async function loadLoginBackdrop() {
    const image = $('login-backdrop-image');
    if (!image) return;
    try {
      const profile = await request('/dashboard/api/bot-profile');
      if (!profile || typeof profile.avatar_url !== 'string') return;
      const url = new URL(profile.avatar_url);
      if (url.protocol !== 'https:' || !['cdn.discordapp.com', 'media.discordapp.net'].includes(url.hostname)) return;
      image.addEventListener('load', () => image.classList.add('loaded'), { once: true });
      image.src = url.href;
    } catch (_) {
      // Sign-in remains usable if the public avatar lookup is temporarily unavailable.
    }
  }
  async function loadStatistics() {
    state.statisticsLoading = true;
    state.statisticsError = null;
    render();
    try {
      const data = await request('/dashboard/api/statistics');
      if (!data || !Array.isArray(data.guilds) || !Number.isInteger(data.total) || data.total !== data.guilds.length) {
        throw new Error('The server returned an incomplete bot server overview. Please try again.');
      }
      state.statistics = data.guilds
        .filter((guild) => guild && guild.id != null)
        .map((guild) => ({ ...guild, id: String(guild.id) }));
      state.statisticsTotal = data.total;
    } catch (error) {
      state.statisticsError = errorMessage(error);
    } finally {
      state.statisticsLoading = false;
      render();
    }
  }
  async function loadGuilds() {
    state.loading = true;
    state.loadError = null;
    state.alert = null;
    render();
    try {
      const data = await request('/dashboard/api/guilds');
      if (!data || !Array.isArray(data.guilds)) throw new Error('The server returned an unexpected server list. Please try again.');
      state.guilds = data.guilds.filter((guild) => guild && guild.id != null).map((guild) => ({ ...guild, id: String(guild.id) }));
      state.guildId = state.guilds.length ? state.guilds[0].id : null;
      state.snapshot = null;
      state.sectionId = null;
      state.baseline.clear();
      state.drafts.clear();
      state.loading = false;
      if (state.guildId) await loadGuild(state.guildId);
      else render();
    } catch (error) {
      state.loading = false;
      state.loadError = { message: errorMessage(error), scope: 'guilds' };
      render();
    }
  }
  async function loadGuild(id) {
    const requestId = ++state.requestId;
    state.loading = true;
    state.loadError = null;
    state.alert = null;
    state.snapshot = null;
    state.sectionId = null;
    state.baseline.clear();
    state.drafts.clear();
    render();
    try {
      const snapshot = await request(`/dashboard/api/guilds/${encodeURIComponent(id)}`);
      if (requestId !== state.requestId) return;
      applySnapshot(snapshot);
      state.loading = false;
      render();
    } catch (error) {
      if (requestId !== state.requestId) return;
      state.loading = false;
      state.loadError = { message: errorMessage(error), scope: 'guild' };
      render();
    }
  }
  function selectGuild() {
    const select = $('server-select');
    const id = select.value;
    if (state.busy || id === state.guildId) { select.value = state.guildId || ''; return; }
    if (hasChanges() && !window.confirm('Discard all unsaved changes and switch servers?')) {
      select.value = state.guildId || '';
      return;
    }
    state.guildId = id;
    loadGuild(id);
  }
  function showStatistics() {
    if (hasChanges() && !window.confirm('You have unsaved changes. Leave the server editor and keep them as a draft?')) return;
    state.activeView = 'statistics';
    state.alert = null;
    render();
    const headingNode = $('page-heading');
    if (headingNode) headingNode.focus({ preventScroll: true });
  }
  function navigate(sectionId) {
    if (state.busy) return;
    state.activeView = 'server';
    state.sectionId = sectionId;
    state.alert = null;
    render();
    const heading = $('page-heading');
    if (heading) heading.focus({ preventScroll: true });
  }
  function refreshNav() {
    const nav = $('section-nav');
    nav.replaceChildren();
    const statsNav = $('statistics-nav');
    statsNav.classList.toggle('active', state.activeView === 'statistics');
    statsNav.disabled = state.busy;
    if (state.activeView === 'statistics') statsNav.setAttribute('aria-current', 'page');
    else statsNav.removeAttribute('aria-current');
    $('overview-nav').classList.toggle('active', state.activeView === 'server' && !state.sectionId);
    $('overview-nav').disabled = state.busy || state.loading || !state.snapshot;
    if (state.activeView === 'server' && !state.sectionId && state.snapshot) $('overview-nav').setAttribute('aria-current', 'page');
    else $('overview-nav').removeAttribute('aria-current');
    if (!state.snapshot) return;
    state.snapshot.sections.forEach((section) => {
      const id = String(section.id);
      const item = button('', 'nav-item' + (id === state.sectionId ? ' active' : ''), () => navigate(id));
      item.disabled = state.busy;
      item.append(icon('settings'), el('span', '', section.title || id));
      if (sectionDirty(section)) {
        const label = el('span', 'nav-unsaved', 'unsaved');
        label.setAttribute('aria-label', 'Unsaved changes in this section');
        item.append(label);
      }
      if (id === state.sectionId) item.setAttribute('aria-current', 'page');
      nav.append(item);
    });
  }
  function render() {
    const select = $('server-select');
    select.replaceChildren();
    if (!state.guilds.length) {
      const option = el('option', '', state.loading ? 'loading servers...' : 'no server selected');
      option.value = '';
      select.append(option);
    } else {
      state.guilds.forEach((guild) => {
        const option = el('option', '', guild.name || 'Unnamed server');
        option.value = guild.id;
        option.selected = guild.id === state.guildId;
        select.append(option);
      });
    }
    select.disabled = state.busy || !state.guilds.length;
    $('logout-button').disabled = state.busy || !state.csrf;
    const guild = state.guilds.find((g) => g.id === state.guildId);
    setAvatar($('picker-avatar'), guild ? guild.name : '?', guild && guild.icon_url);
    refreshNav();
    const target = $('workspace-content');
    target.replaceChildren();
    const wrap = el('div', 'content-wrap');
    target.append(wrap);
    if (!state.loading && state.alert) wrap.append(alertBox(state.alert.kind, state.alert.message));
    if (!state.loading && !state.csrf) wrap.append(alertBox('warning', 'Your session is missing its security token. Editing, refreshing rules, and signing out are unavailable. Reload the page or sign in again.'));
    if (state.activeView === 'statistics') {
      renderStatistics(wrap);
      return;
    }
    if (state.loading) {
      const loading = el('div', 'loading-content');
      loading.setAttribute('role', 'status');
      loading.append(el('span', 'spinner'), el('span', '', 'loading your workspace...'));
      wrap.append(loading);
      return;
    }
    if (state.loadError) {
      wrap.append(breadcrumb('dashboard', 'workspace'));
      const retry = () => state.loadError.scope === 'guilds' ? loadGuilds() : loadGuild(state.guildId);
      const box = emptyState('couldn’t load your workspace', state.loadError.message, 'try again', retry);
      if (state.loadError.message.startsWith('Your session has expired.')) box.append(loginLink());
      wrap.append(box);
      return;
    }
    if (!state.guilds.length) {
      wrap.append(breadcrumb('dashboard', 'workspace'));
      wrap.append(emptyState('no servers available', 'There aren’t any servers available to manage right now. Check that Yuki is in your server and that you have the required permissions.', 'check again', loadGuilds));
      return;
    }
    if (!state.snapshot) return;
    const section = currentSection();
    wrap.append(breadcrumb('dashboard', state.snapshot.guild.name || 'server', section ? section.title : 'overview'));
    if (section) renderSection(wrap, section);
    else renderOverview(wrap);
  }
  function breadcrumb(...parts) {
    const row = el('div', 'breadcrumb');
    row.setAttribute('aria-label', 'Breadcrumb');
    parts.forEach((part, index) => {
      if (index) row.append(el('span', 'slash', '/'));
      row.append(el('span', index === parts.length - 1 ? 'current' : '', part || 'server'));
    });
    return row;
  }
  function alertBox(kind, message) {
    const box = el('div', 'alert ' + (kind || ''));
    box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    const content = el('span', 'alert-message', message);
    if (message.startsWith('Your session has expired.') || message.startsWith('Your session is missing its security token.')) content.append(loginLink());
    box.append(el('span', 'alert-icon', kind === 'success' ? '✓' : '!'), content);
    if (state.alert && state.alert.message === message) {
      const close = button('×', 'alert-close', () => { state.alert = null; render(); });
      close.setAttribute('aria-label', 'Dismiss message');
      box.append(close);
    }
    return box;
  }
  function loginLink() {
    const link = el('a', 'alert-link', 'sign in again ↗');
    link.href = '/dashboard/login';
    return link;
  }
  function emptyState(title, description, action, handler) {
    const box = el('div', 'panel empty-state');
    box.append(el('h2', '', title), el('p', '', description));
    if (action) box.append(button(action, 'button', handler));
    return box;
  }
  function heading(title, description = '') {
    const row = el('div', 'page-heading');
    const copyNode = el('div', 'heading-copy');
    const titleNode = el('h1', '', title);
    titleNode.id = 'page-heading';
    titleNode.tabIndex = -1;
    copyNode.append(titleNode);
    if (description) copyNode.append(el('p', '', description));
    row.append(copyNode);
    return row;
  }
  function renderOverview(wrap) {
    const page = heading('Overview');
    const actions = el('div', 'heading-actions');
    const refresh = button('refresh server rules', 'button', refreshRules);
    refresh.prepend(icon('refresh'));
    refresh.disabled = state.busy || !state.csrf;
    actions.append(refresh);
    page.append(actions);
    wrap.append(page);
    if (hasChanges()) wrap.append(alertBox('warning', 'You have unsaved changes in your settings. Return to a section to save or discard them.'));
    const panel = el('div', 'panel server-summary');
    const main = el('div', 'server-summary-main');
    const avatar = el('span', 'server-avatar avatar');
    setAvatar(avatar, state.snapshot.guild.name, state.snapshot.guild.icon_url);
    const info = el('div');
    info.append(el('div', 'small-label', 'SERVER'), el('h2', '', state.snapshot.guild.name || 'Unnamed server'));
    main.append(avatar, info);
    panel.append(main);
    wrap.append(panel);
    const sections = state.snapshot.sections;
    const top = el('div', 'overview-title');
    top.append(el('h2', '', 'Server settings'));
    wrap.append(top);
    if (!sections.length) {
      wrap.append(emptyState('no settings available', 'There are no settings sections available for this server right now.'));
    } else {
      const grid = el('div', 'section-grid');
      sections.forEach((section) => {
        const card = button('', 'panel section-card', () => navigate(String(section.id)));
        card.disabled = state.busy;
        const cardTop = el('div', 'section-card-top');
        const cardIcon = el('span', 'section-icon');
        cardIcon.append(icon('settings'));
        cardTop.append(cardIcon, el('span', 'card-arrow', '↗'));
        card.append(cardTop, el('strong', '', section.title || String(section.id)), el('p', '', section.description || 'Manage this section’s settings.'));
        if (sectionDirty(section)) {
          const label = el('span', 'unsaved-label', 'unsaved');
          label.setAttribute('aria-label', 'Unsaved changes in this section');
          card.append(label);
        }
        grid.append(card);
      });
      wrap.append(grid);
    }
  }
  function renderStatistics(wrap) {
    wrap.append(breadcrumb('dashboard', 'statistics overview'));
    wrap.append(heading('Statistics overview', 'Every server Yuki is currently in. This inventory is read-only; server settings remain limited to servers you can manage.'));
    if (state.statisticsLoading) {
      const loading = el('div', 'loading-content');
      loading.setAttribute('role', 'status');
      loading.append(el('span', 'spinner'), el('span', '', 'loading bot server statistics...'));
      wrap.append(loading);
      return;
    }
    if (state.statisticsError) {
      wrap.append(emptyState('couldn’t load bot statistics', state.statisticsError, 'try again', loadStatistics));
      return;
    }
    const summary = el('div', 'panel statistics-summary');
    summary.append(el('div', 'small-label', 'TOTAL SERVERS'), el('div', 'statistics-total', String(state.statisticsTotal)));
    wrap.append(summary);
    const title = el('div', 'overview-title');
    title.append(el('h2', '', 'All servers'));
    wrap.append(title);
    if (!state.statistics.length) {
      wrap.append(emptyState('Yuki isn’t in any servers yet', 'When Yuki joins a Discord server, it will appear here.'));
      return;
    }
    const list = el('div', 'panel statistics-list');
    state.statistics.forEach((guild) => {
      const row = el('div', 'statistics-row');
      const avatar = el('span', 'statistics-avatar avatar');
      setAvatar(avatar, guild.name, guild.icon_url);
      const copyNode = el('div', 'statistics-copy');
      copyNode.append(el('strong', '', guild.name || 'Unnamed server'), el('span', '', 'Yuki is a member'));
      row.append(avatar, copyNode, el('span', 'statistics-readonly', 'read only'));
      list.append(row);
    });
    wrap.append(list);
  }
  function renderSection(wrap, section) {
    const page = heading(section.title || String(section.id), section.description || 'Manage the settings in this section.');
    wrap.append(page);
    const panel = el('div', 'panel settings-panel');
    const head = el('div', 'settings-panel-head');
    const headCopy = el('div');
    headCopy.append(el('h2', 'panel-title', 'Settings'), el('p', '', 'Changes apply after saving.'));
    head.append(headCopy);
    panel.append(head);
    const form = el('fieldset', 'field-list');
    form.disabled = state.busy;
    form.setAttribute('aria-label', `${section.title || 'Section'} settings`);
    if (!section.fields.length) {
      const empty = el('div', 'empty-state');
      empty.append(el('p', '', 'There are no settings in this section right now.'));
      form.append(empty);
    }
    section.fields.forEach((field, index) => form.append(renderField(field, index)));
    panel.append(form);
    const actions = el('div', 'panel-actions');
    const status = el('div', 'change-status');
    status.id = 'change-status';
    status.setAttribute('aria-live', 'polite');
    const actionButtons = el('div', 'action-buttons');
    const cancel = button('cancel', 'button', () => cancelSection(section));
    cancel.id = 'cancel-button';
    const save = button('save changes', 'button button-dark', () => saveSection(section));
    save.id = 'save-button';
    actionButtons.append(cancel, save);
    actions.append(status, actionButtons);
    panel.append(actions);
    wrap.append(panel);
    syncDirtyUI();
  }
  function renderRulesEditor(field, id, disabled, value) {
    const editor = el('div', 'rules-editor');
    editor.setAttribute('role', 'group');
    const rules = Array.isArray(value) ? value : [];
    if (!rules.length) {
      const empty = el('div', 'rules-editor-empty');
      empty.append(el('strong', '', 'No parsed rules yet'), el('p', '', 'When Yuki finds rules in this server, their original wording and editable corrections will appear here.'));
      editor.append(empty);
      return editor;
    }

    const options = Array.isArray(field.options) ? field.options.map((option) => ({
      value: String(option.value), label: option.label == null ? String(option.value) : String(option.label)
    })) : [];
    rules.forEach((rule, index) => {
      if (!rule || typeof rule !== 'object') return;
      const card = el('article', 'rule-card');
      if (rule.stale) card.classList.add('rule-card-stale');
      const heading = el('div', 'rule-card-heading');
      const title = el('h3', 'rule-number', `Rule ${rule.n == null ? index + 1 : rule.n}`);
      heading.append(title);
      card.append(heading);
      if (rule.stale) {
        card.append(el('p', 'rule-stale-note', 'Stale / orphan rule. To remove its saved correction, clear the correction and guidance, choose “auto”, then save.'));
      }

      const original = el('p', 'rule-original', rule.text == null ? '' : rule.text);
      original.setAttribute('aria-label', 'Original parsed rule text (read only)');
      card.append(original);

      const metadata = el('div', 'rule-metadata');
      const source = el('div', 'rule-meta-item');
      source.append(el('span', 'rule-meta-label', 'Source channel'), el('span', 'rule-meta-value', rule.source_channel == null || rule.source_channel === '' ? 'Not available' : rule.source_channel));
      const category = el('div', 'rule-meta-item');
      category.append(el('span', 'rule-meta-label', 'Parsed category'), el('span', 'rule-meta-value', rule.category == null || rule.category === '' ? 'Not categorized' : rule.category));
      metadata.append(source, category);
      card.append(metadata);

      const correctionId = `${id}-rule-${index}-correction`;
      const correctionLabel = el('label', 'rule-control-label', 'Correction');
      correctionLabel.htmlFor = correctionId;
      const correction = el('textarea', 'text-area rule-editor-control');
      correction.id = correctionId;
      correction.rows = 3;
      correction.disabled = disabled;
      correction.value = rule.correction == null ? '' : String(rule.correction);
      correction.addEventListener('input', () => updateRule(field, rule.id, 'correction', correction.value));
      const guidanceId = `${id}-rule-${index}-guidance`;
      const guidanceLabel = el('label', 'rule-control-label', 'Guidance for Yuki (optional)');
      guidanceLabel.htmlFor = guidanceId;
      const guidance = el('textarea', 'text-area rule-editor-control rule-guidance');
      guidance.id = guidanceId;
      guidance.rows = 2;
      guidance.disabled = disabled;
      guidance.value = rule.guidance == null ? '' : String(rule.guidance);
      guidance.addEventListener('input', () => updateRule(field, rule.id, 'guidance', guidance.value));

      const categoryId = `${id}-rule-${index}-category-override`;
      const categoryLabel = el('label', 'rule-control-label', 'Category override');
      categoryLabel.htmlFor = categoryId;
      const categorySelect = el('select', 'select-input rule-editor-select');
      categorySelect.id = categoryId;
      categorySelect.disabled = disabled;
      const currentOverride = rule.category_override == null ? 'auto' : String(rule.category_override);
      let matchedOverride = false;
      options.forEach((option) => {
        const optionNode = el('option', '', option.label);
        optionNode.value = option.value;
        optionNode.selected = option.value === currentOverride;
        if (optionNode.selected) matchedOverride = true;
        categorySelect.append(optionNode);
      });
      if (!matchedOverride) {
        const current = el('option', '', currentOverride || 'auto');
        current.value = currentOverride;
        current.selected = true;
        categorySelect.prepend(current);
      }
      categorySelect.addEventListener('change', () => updateRule(field, rule.id, 'category_override', categorySelect.value));

      const controls = el('div', 'rule-controls');
      const correctionGroup = el('div', 'rule-control-group');
      correctionGroup.append(correctionLabel, correction);
      const guidanceGroup = el('div', 'rule-control-group');
      guidanceGroup.append(guidanceLabel, guidance);
      const categoryGroup = el('div', 'rule-control-group rule-category-group');
      categoryGroup.append(categoryLabel, categorySelect);
      controls.append(correctionGroup, guidanceGroup, categoryGroup);
      card.append(controls);
      editor.append(card);
    });
    return editor;
  }
  function updateRule(field, ruleId, property, nextValue) {
    if (state.busy || readonly(field) || typeof field.key !== 'string') return;
    const current = state.drafts.get(field.key);
    if (!Array.isArray(current)) return;
    const updated = current.map((rule) => rule && rule.id === ruleId ? { ...rule, [property]: nextValue } : rule);
    state.drafts.set(field.key, copy(updated));
    syncDirtyUI();
  }
  function renderField(field, index) {
    const row = el('div', 'field-row');
    const detail = el('div');
    const id = `setting-${index}`;
    const label = el('label', 'field-label', field.label || field.key || 'Setting');
    label.id = `${id}-label`;
    if (field.type !== 'rules_editor') label.htmlFor = id;
    detail.append(label);
    if (field.description) {
      const description = el('p', 'field-description', field.description);
      description.id = `${id}-description`;
      detail.append(description);
    }
    const control = el('div', 'field-control');
    const disabled = readonly(field) || typeof field.key !== 'string';
    const value = state.drafts.get(field.key);
    const update = (next) => {
      if (disabled || state.busy) return;
      state.drafts.set(field.key, copy(next));
      syncDirtyUI();
    };
    const describe = (input) => {
      input.id = id;
      input.setAttribute('aria-labelledby', label.id);
      const ids = [];
      if (field.description) ids.push(`${id}-description`);
      if (disabled) ids.push(`${id}-permission`);
      if (field.type === 'lines') ids.push(`${id}-hint`);
      if (ids.length) input.setAttribute('aria-describedby', ids.join(' '));
      input.disabled = disabled;
    };
    if (field.type === 'rules_editor') {
      const editor = renderRulesEditor(field, id, disabled, value);
      editor.id = id;
      editor.setAttribute('aria-labelledby', label.id);
      if (field.description) editor.setAttribute('aria-describedby', `${id}-description`);
      control.append(editor);
    } else if (field.type === 'boolean') {
      const switchLabel = el('label', 'toggle-label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'toggle-input';
      describe(input);
      input.checked = value === true;
      input.indeterminate = value == null;
      const track = el('span', 'toggle-track');
      track.setAttribute('aria-hidden', 'true');
      const stateLabel = el('span', 'toggle-state', value == null ? 'not set' : input.checked ? 'on' : 'off');
      input.addEventListener('change', () => { stateLabel.textContent = input.checked ? 'on' : 'off'; update(input.checked); });
      switchLabel.append(input, track, stateLabel);
      control.append(switchLabel);
    } else if (field.type === 'select') {
      const select = el('select', 'select-input');
      describe(select);
      const options = Array.isArray(field.options) ? field.options : [];
      let matched = false;
      options.forEach((option) => {
        const opt = el('option', '', option.label == null ? String(option.value) : option.label);
        opt.value = String(option.value);
        if (value != null && opt.value === String(value)) { opt.selected = true; matched = true; }
        select.append(opt);
      });
      if (!matched) {
        const opt = el('option', '', value == null ? 'select an option' : String(value));
        opt.value = value == null ? '' : String(value);
        opt.selected = true;
        opt.disabled = value == null;
        select.prepend(opt);
      }
      select.addEventListener('change', () => update(select.value));
      control.append(select);
    } else if (field.type === 'multi_select') {
      const group = el('div', 'multi-options');
      group.id = id;
      group.setAttribute('role', 'group');
      group.setAttribute('aria-labelledby', label.id);
      if (field.description) group.setAttribute('aria-describedby', `${id}-description`);
      const options = Array.isArray(field.options) ? field.options.map((option) => ({ value: String(option.value), label: option.label == null ? String(option.value) : String(option.label) })) : [];
      const selected = Array.isArray(value) ? value.map(String) : [];
      selected.forEach((item) => { if (!options.some((option) => option.value === item)) options.push({ value: item, label: `${item} (current selection)` }); });
      if (!options.length) group.append(el('div', 'empty-options', 'No options are available.'));
      options.forEach((option) => {
        const optionLabel = el('label', 'multi-option');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = option.value;
        input.checked = selected.includes(option.value);
        input.disabled = disabled;
        input.addEventListener('change', () => {
          const previous = Array.isArray(state.drafts.get(field.key)) ? state.drafts.get(field.key).slice() : [];
          const next = input.checked ? (previous.includes(option.value) ? previous : [...previous, option.value]) : previous.filter((item) => item !== option.value);
          update(next);
        });
        optionLabel.append(input, el('span', '', option.label));
        group.append(optionLabel);
      });
      control.append(group);
    } else if (field.type === 'lines') {
      const area = el('textarea', 'text-area');
      describe(area);
      area.value = Array.isArray(value) ? value.join('\n') : '';
      area.spellcheck = false;
      area.addEventListener('input', () => update(area.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)));
      const hint = el('p', 'field-hint', 'One item per line. Leave empty to clear the list.');
      hint.id = `${id}-hint`;
      control.append(area, hint);
    } else if (field.type === 'text') {
      const longText = /note|prompt|message|instruction|description|context/i.test(`${field.key} ${field.label || ''}`);
      const input = el(longText ? 'textarea' : 'input', longText ? 'text-area' : 'text-input');
      if (!longText) input.type = 'text';
      describe(input);
      input.value = value == null ? '' : String(value);
      input.addEventListener('input', () => update(input.value));
      control.append(input);
    } else {
      control.append(el('p', 'field-hint', 'This setting type cannot be edited here.'));
    }
    if (disabled) {
      const reason = typeof field.read_only_reason === 'string' ? field.read_only_reason
        : typeof field.disabled_reason === 'string' ? field.disabled_reason
          : 'Read only · Editing is unavailable with your current permissions.';
      const hint = el('p', 'field-hint', reason);
      hint.id = `${id}-permission`;
      control.append(hint);
    }
    row.append(detail, control);
    return row;
  }
  function syncDirtyUI() {
    const section = currentSection();
    if (!section) return;
    const count = section.fields.filter(hasDraftChange).length;
    const editableCount = section.fields.filter(isDirty).length;
    const status = $('change-status');
    if (status) {
      status.classList.toggle('dirty', count > 0);
      const locked = count - editableCount;
      const message = count ? `${count} unsaved ${count === 1 ? 'change' : 'changes'}${locked ? ` · ${locked} read-only` : ''}` : 'All changes saved';
      status.textContent = message;
    }
    if ($('cancel-button')) $('cancel-button').disabled = state.busy || !count;
    if ($('save-button')) $('save-button').disabled = state.busy || !editableCount || !state.csrf;
    refreshNav();
  }
  function cancelSection(section) {
    if (state.busy || !sectionDirty(section)) return;
    if (!window.confirm('Discard unsaved changes in this section?')) return;
    section.fields.forEach((field) => {
      if (state.baseline.has(field.key)) {
        state.baseline.set(field.key, copy(field.value));
        state.drafts.set(field.key, copy(field.value));
      }
    });
    setAlert('success', 'Changes discarded.');
    render();
  }
  async function saveSection(section) {
    if (state.busy || !state.csrf) return;
    const changed = section.fields.filter(isDirty);
    if (!changed.length) return;
    const changes = Object.create(null);
    changed.forEach((field) => { changes[field.key] = copy(state.drafts.get(field.key)); });
    const expected = Object.create(null);
    section.fields.forEach((field) => {
      if (typeof field.key === 'string' && state.baseline.has(field.key)) expected[field.key] = copy(state.baseline.get(field.key));
    });
    const savedKeys = new Set(changed.map((field) => field.key));
    const preserve = new Set(fields().filter((field) => hasDraftChange(field) && !savedKeys.has(field.key)).map((field) => field.key));
    state.busy = true;
    state.alert = null;
    render();
    try {
      const snapshot = await request(`/dashboard/api/guilds/${encodeURIComponent(state.guildId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf }, body: JSON.stringify({ changes, expected })
      });
      applySnapshot(snapshot, preserve);
      setAlert('success', 'Changes saved.');
    } catch (error) {
      setAlert('error', errorMessage(error));
    } finally {
      state.busy = false;
      render();
    }
  }
  async function refreshRules() {
    if (state.busy || !state.csrf) return;
    state.busy = true;
    state.alert = null;
    render();
    try {
      const data = await request(`/dashboard/api/guilds/${encodeURIComponent(state.guildId)}/refresh`, {
        method: 'POST', headers: { 'X-CSRF-Token': state.csrf }, optionalJson: true
      });
      const message = data && typeof data.message === 'string' ? data.message : 'Server rules refreshed.';
      const preserve = new Set(fields().filter(hasDraftChange).map((field) => field.key));
      try {
        const snapshot = await request(`/dashboard/api/guilds/${encodeURIComponent(state.guildId)}`);
        applySnapshot(snapshot, preserve);
        setAlert('success', message);
      } catch (error) {
        setAlert('warning', `Server rules were refreshed, but the latest settings could not be loaded. Your unsaved changes are still here. ${errorMessage(error)}`);
      }
    } catch (error) {
      setAlert('error', errorMessage(error));
    } finally {
      state.busy = false;
      render();
    }
  }
  async function logout() {
    if (state.busy || !state.csrf) return;
    if (hasChanges() && !window.confirm('Discard all unsaved changes and sign out?')) return;
    state.busy = true;
    render();
    try {
      await request('/dashboard/logout', { method: 'POST', headers: { 'X-CSRF-Token': state.csrf }, expectNoBody: true });
      state.drafts = new Map(state.baseline);
      window.location.reload();
    } catch (error) {
      state.busy = false;
      setAlert('error', errorMessage(error));
      render();
    }
  }
  $('server-select').addEventListener('change', selectGuild);
  $('overview-nav').addEventListener('click', () => navigate(null));
  $('statistics-nav').addEventListener('click', showStatistics);
  $('logout-button').addEventListener('click', logout);
  window.addEventListener('beforeunload', (event) => {
    if (!hasChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  start();
})();
