/**
 * EthOS Download Manager - Browser Extension Background Script
 */

// Context menu setup
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'download-to-ethos',
    title: 'Download to EthOS',
    contexts: ['link', 'video', 'audio', 'image']
  });

  chrome.contextMenus.create({
    id: 'download-page-to-ethos',
    title: 'Download this page to EthOS',
    contexts: ['page']
  });
});

// Context menu click handler
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'download-to-ethos') {
    const url = info.linkUrl || info.srcUrl;
    if (url) {
      sendToEthOS(url, tab.title, tab.url);
    }
  } else if (info.menuItemId === 'download-page-to-ethos') {
    sendToEthOS(tab.url, tab.title, tab.url);
  }
});

// Listen for messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'sendDownload') {
    sendToEthOS(request.url, request.pageTitle, request.pageUrl)
      .then(result => sendResponse({ success: true, result }))
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true; // Keep channel open for async response
  } else if (request.action === 'testConnection') {
    testConnection()
      .then(result => sendResponse({ success: true }))
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true;
  }
});

// Send download to EthOS
async function sendToEthOS(url, pageTitle, pageUrl) {
  const config = await getConfig();
  
  if (!config.server || !config.token) {
    throw new Error('EthOS server and API token must be configured. Click the extension icon to set up.');
  }

  const apiUrl = `${config.server}/api/downloads/browser-add`;
  
  const response = await fetch(apiUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-EthOS-Token': config.token
    },
    body: JSON.stringify({
      url: url,
      page_title: pageTitle,
      page_url: pageUrl,
      use_debrid: config.useDebrid !== false
    })
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `Server returned ${response.status}`);
  }

  const result = await response.json();
  
  // Show notification
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icon128.png',
    title: 'EthOS Download Manager',
    message: `Download added: ${url.substring(0, 60)}${url.length > 60 ? '...' : ''}`
  });

  return result;
}

// Test connection to EthOS server
async function testConnection() {
  const config = await getConfig();
  
  if (!config.server) {
    throw new Error('Server URL not configured');
  }

  const response = await fetch(`${config.server}/api/downloads/config`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json'
    }
  });

  if (!response.ok) {
    throw new Error(`Cannot connect to EthOS server (${response.status})`);
  }

  return true;
}

// Get extension configuration
async function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(['server', 'token', 'useDebrid'], (items) => {
      resolve({
        server: items.server || '',
        token: items.token || '',
        useDebrid: items.useDebrid !== false
      });
    });
  });
}

// Save configuration
async function saveConfig(config) {
  return new Promise((resolve) => {
    chrome.storage.sync.set(config, resolve);
  });
}
