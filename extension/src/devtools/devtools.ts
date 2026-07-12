// devtools_page entry: register the Sentinel panel in the DevTools window. This runs once when DevTools
// opens for a tab; the panel's own logic lives in panel.ts (loaded by panel.html).
chrome.devtools.panels.create('Sentinel', '', 'panel.html', () => {
  if (chrome.runtime.lastError) {
    console.error('[sentinel] failed to create DevTools panel:', chrome.runtime.lastError.message);
  }
});
