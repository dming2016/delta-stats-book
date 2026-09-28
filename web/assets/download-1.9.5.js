(() => {
  const installerFilePattern = /^DeltaStatsAssistant-[A-Za-z0-9][A-Za-z0-9._+-]*-Setup\.exe$/;
  const portableFilePattern = /^DeltaStatsAssistant-[A-Za-z0-9][A-Za-z0-9._+-]*\.zip$/;
  function updateLinks(selector, filename) {
    const url = new URL(`downloads/${filename}`, document.baseURI).toString();
    document.querySelectorAll(selector).forEach(link => { link.href = url; });
  }
  updateLinks('[data-installer-download]', 'DeltaStatsAssistant-Setup.exe');
  updateLinks('[data-portable-download]', 'DeltaStatsAssistant.zip');
  document.getElementById('copyright').textContent = `© ${new Date().getFullYear()}`;
  if (window.lucide) window.lucide.createIcons({attrs: {'stroke-width': 1.7}});
  const menu = document.getElementById('siteMenuButton');
  const nav = document.getElementById('siteNav');
  function closeMenu() {
    nav.classList.remove('is-open');
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', '展开导航');
  }
  menu.addEventListener('click', () => {
    const open = nav.classList.toggle('is-open');
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? '收起导航' : '展开导航');
  });
  nav.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && nav.classList.contains('is-open')) { closeMenu(); menu.focus(); }
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.site-header')) closeMenu();
  });
  const tabs = [...document.querySelectorAll('[data-preview]')];
  function selectTab(selected, focus = false) {
    tabs.forEach(tab => {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    });
    if (focus) selected.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      let next = null;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== null) { event.preventDefault(); selectTab(tabs[next], true); }
    });
  });
  async function loadRelease() {
    try {
      const response = await fetch(new URL('updates/version.json', document.baseURI), {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('Release unavailable');
      const release = await response.json();
      if (release.version) document.querySelectorAll('[data-version]').forEach(node => { node.textContent = `v${release.version}`; });
      if (typeof release.installer_file === 'string' && installerFilePattern.test(release.installer_file)) updateLinks('[data-installer-download]', release.installer_file);
      if (typeof release.download_file === 'string' && portableFilePattern.test(release.download_file)) updateLinks('[data-portable-download]', release.download_file);
      for (const [field, selector, label] of [
        ['installer_size', '[data-installer-meta]', 'EXE 安装程序'],
        ['download_size', '[data-portable-meta]', 'ZIP 压缩包'],
      ]) {
        if (Number(release[field]) > 0) document.querySelector(selector).textContent = `${label} · ${(Number(release[field]) / 1024 / 1024).toFixed(1)} MB · Windows 10 / 11`;
      }
    } catch (_) {
      document.querySelectorAll('[data-version]').forEach(node => { node.textContent = 'Windows 桌面版'; });
    }
  }
  loadRelease();
})();
