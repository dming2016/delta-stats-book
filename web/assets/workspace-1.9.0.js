(() => {
  if (window.lucide) window.lucide.createIcons({attrs: {'stroke-width': 1.7}});

  document.querySelectorAll('.workspace-rail button').forEach((button) => {
    button.title = button.textContent.trim();
    button.setAttribute('aria-label', button.title);
  });
  document.querySelectorAll('[data-workspace-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.workspaceAction;
      if (action === 'accounts') document.getElementById('authState').click();
      if (action === 'friends') document.getElementById('manageFriendsButton').click();
      if (action === 'about') document.querySelector('[data-app-command="about"]').click();
    });
  });

  document.getElementById('resetFiltersButton').addEventListener('click', () => {
    state.modeFilters[state.mode] = createDefaultModeFilters()[state.mode];
    restoreModeFilters(state.mode);
    renderModeLayout();
    document.getElementById('customTime').open = false;
    document.getElementById('sessionPicker').open = false;
    loadData();
  });

  const customTime = document.getElementById('customTime');
  const sessionPicker = document.getElementById('sessionPicker');
  customTime.addEventListener('toggle', () => {
    if (customTime.open) sessionPicker.open = false;
  });
  sessionPicker.addEventListener('toggle', () => {
    if (sessionPicker.open) customTime.open = false;
  });
  document.getElementById('applyButton').addEventListener('click', () => {
    customTime.open = false;
  });
  document.addEventListener('click', (event) => {
    if (!customTime.contains(event.target)) customTime.open = false;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && customTime.open) {
      customTime.open = false;
      customTime.querySelector('summary').focus();
    }
  });
})();
