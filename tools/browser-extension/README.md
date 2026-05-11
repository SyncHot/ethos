# EthOS Download Manager - Browser Extension

Send downloads directly from your browser to your EthOS NAS!

## Features

- **Right-click menu** - Download any link, image, video, or audio directly to EthOS
- **One-click download** - Add current page or pasted URLs
- **Debrid support** - Automatically uses your configured debrid service
- **Notifications** - Get notified when downloads are added

## Installation

### Chrome / Edge / Brave

1. Open `chrome://extensions/` (or `edge://extensions/`)
2. Enable "Developer mode" (toggle in top-right)
3. Click "Load unpacked"
4. Select the `/opt/ethos/tools/browser-extension` folder
5. Done!

### Firefox

1. Open `about:debugging#/runtime/this-firefox`
2. Click "Load Temporary Add-on"
3. Select `manifest.json` from `/opt/ethos/tools/browser-extension/`
4. Done!

## Setup

1. **Click the extension icon** in your browser toolbar
2. **Enter your EthOS server URL** (e.g., `http://192.168.1.100` or `http://nas.local`)
3. **Generate API token in EthOS**:
   - Open Download Manager in EthOS
   - Go to Settings tab
   - Click "Browser Extension" section
   - Click "Generate Token"
   - Copy the token
4. **Paste the token** in the extension popup
5. Click "Test Connection" to verify
6. Click "Save Settings"

## Usage

### Method 1: Right-click menu
- Right-click any link, image, video, or audio
- Select "Download to EthOS"
- Done! Download will start on your NAS

### Method 2: Extension popup
- Click the extension icon
- Paste URL(s) in the text area (one per line)
- Click "Add to Downloads"

## Icons

The extension currently uses placeholder icons. To add proper icons:

1. Create PNG images (16x16, 48x48, 128x128)
2. Save as `icon16.png`, `icon48.png`, `icon128.png` in the extension folder
3. Reload the extension

## Troubleshooting

**"Invalid or missing API token"**
- Make sure you generated a token in EthOS Download Manager settings
- Copy the exact token (no extra spaces)
- Try generating a new token

**"Cannot connect to EthOS server"**
- Verify the server URL is correct
- Make sure EthOS is accessible from your browser
- Check firewall settings
- Try with IP address instead of hostname

**Downloads not appearing in EthOS**
- Check the EthOS Download Manager (should show immediately)
- Verify you're logged in to EthOS
- Check the Event Log for errors

## Privacy

This extension:
- **Does NOT collect any data**
- **Does NOT track your browsing**
- Only sends URLs you explicitly choose to download to YOUR EthOS server
- All communication is direct between your browser and your NAS (no third parties)
