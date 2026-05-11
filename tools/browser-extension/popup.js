/**
 * EthOS Download Manager - Popup Script
 */

document.addEventListener('DOMContentLoaded', () => {
  // Load saved config
  chrome.storage.sync.get(['server', 'token', 'useDebrid'], (items) => {
    document.getElementById('server').value = items.server || '';
    document.getElementById('token').value = items.token || '';
    document.getElementById('useDebrid').checked = items.useDebrid !== false;
  });

  // Save button
  document.getElementById('saveBtn').addEventListener('click', async () => {
    const server = document.getElementById('server').value.trim().replace(/\/$/, '');
    const token = document.getElementById('token').value.trim();
    const useDebrid = document.getElementById('useDebrid').checked;

    if (!server) {
      showStatus('error', 'Server URL is required');
      return;
    }

    chrome.storage.sync.set({ server, token, useDebrid }, () => {
      showStatus('success', 'Settings saved!');
    });
  });

  // Test button
  document.getElementById('testBtn').addEventListener('click', async () => {
    const btn = document.getElementById('testBtn');
    const server = document.getElementById('server').value.trim().replace(/\/$/, '');
    
    if (!server) {
      showStatus('error', 'Server URL is required');
      return;
    }
    
    btn.disabled = true;
    btn.textContent = 'Testing...';

    try {
      // Test connection directly from popup
      const response = await fetch(`${server}/api/downloads/config`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        throw new Error(`Cannot connect to EthOS server (${response.status})`);
      }

      showStatus('success', 'Connection successful!');
    } catch (error) {
      showStatus('error', `Connection failed: ${error.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Test Connection';
    }
  });

  // Add download button
  document.getElementById('addBtn').addEventListener('click', async () => {
    const urls = document.getElementById('urlInput').value
      .trim()
      .split('\n')
      .map(u => u.trim())
      .filter(u => u);

    if (urls.length === 0) {
      showDownloadStatus('error', 'No URLs provided');
      return;
    }

    const btn = document.getElementById('addBtn');
    btn.disabled = true;
    btn.textContent = 'Adding...';

    try {
      // Get current tab info
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

      for (const url of urls) {
        await new Promise((resolve, reject) => {
          chrome.runtime.sendMessage({
            action: 'sendDownload',
            url: url,
            pageTitle: tab.title,
            pageUrl: tab.url
          }, (response) => {
            if (response.success) {
              resolve();
            } else {
              reject(new Error(response.error));
            }
          });
        });
      }

      showDownloadStatus('success', `${urls.length} download(s) added!`);
      document.getElementById('urlInput').value = '';
    } catch (error) {
      showDownloadStatus('error', `Failed: ${error.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Add to Downloads';
    }
  });

  // Try to get current page URL and put it in urlInput
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0] && tabs[0].url && tabs[0].url.startsWith('http')) {
      // Don't auto-fill, but make it easy to use
      document.getElementById('urlInput').placeholder = 
        'Paste URL(s) here, or use right-click menu on links';
    }
  });
});

function showStatus(type, message) {
  const status = document.getElementById('status');
  status.className = `status ${type}`;
  status.textContent = message;
  status.style.display = 'block';
  setTimeout(() => {
    status.style.display = 'none';
  }, 3000);
}

function showDownloadStatus(type, message) {
  const status = document.getElementById('downloadStatus');
  status.className = `status ${type}`;
  status.textContent = message;
  status.style.display = 'block';
  setTimeout(() => {
    status.style.display = 'none';
  }, 3000);
}
