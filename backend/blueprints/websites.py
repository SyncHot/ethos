"""
EthOS — Websites (Simple CMS / Site Builder)
Allows users to create and manage static websites
with a simple visual editor and templates.
"""

import os
import json
import uuid
import shutil
import time
import re
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path
from utils import register_pkg_routes

log = logging.getLogger('websites')

websites_bp = Blueprint('websites', __name__, url_prefix='/api/websites')

# ── Data paths ──
_DATA_DIR = data_path('websites')
_SITES_DIR = os.path.join(_DATA_DIR, 'sites')
_SITES_META = os.path.join(_DATA_DIR, 'sites.json')

def _ensure_dirs():
    os.makedirs(_SITES_DIR, exist_ok=True)
    if not os.path.isfile(_SITES_META):
        with open(_SITES_META, 'w') as f:
            json.dump([], f)
        _seed_ethos_site()

def _seed_ethos_site():
    """Create a demo promotional site for EthOS on first run."""
    site_id = 'ethos'
    now = datetime.now().isoformat()
    year = datetime.now().year

    pages = [
        {
            'slug': 'index',
            'title': 'Home Page',
            'content': '''
<section class="hero-landing">
  <div class="hero-glow"></div>
  <div class="hero-grid-bg"></div>
  <div class="hero-content">
    <div class="hero-badge">Open Source &middot; Self-Hosted &middot; Private</div>
    <h1 class="hero-title">
      Your server.<br>
      <span class="gradient-text">Your rules.</span>
    </h1>
    <p class="hero-desc">
      EthOS is a complete operating system for your home NAS.<br>
      Files, backups, monitoring, multimedia, printer — everything<br>
      in one beautiful interface, without cloud, without subscription.
    </p>
    <div class="hero-actions">
      <a href="installation.html" class="btn-primary btn-lg">Install for free <span class="btn-arrow">→</span></a>
      <a href="features.html" class="btn-ghost">See features</a>
    </div>
    <div class="hero-stats">
      <div class="stat"><span class="stat-num">25+</span><span class="stat-label">Apps</span></div>
      <div class="stat-sep"></div>
      <div class="stat"><span class="stat-num">Free</span><span class="stat-label">Forever</span></div>
      <div class="stat-sep"></div>
      <div class="stat"><span class="stat-num">100%</span><span class="stat-label">Private</span></div>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header">
    <span class="section-tag">Ecosystem</span>
    <h2>Everything you need</h2>
    <p class="section-desc">One system. Dozens of possibilities. Zero compromises.</p>
  </div>
  <div class="bento">
    <div class="bento-item bento-wide card-glow">
      <div class="bento-icon"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg></div>
      <h3>File Manager</h3>
      <p>Drag &amp; drop, preview, sharing — like Finder, but in your browser. Supports multiple drives simultaneously.</p>
    </div>
    <div class="bento-item card-glow">
      <div class="bento-icon" style="color:#f59e0b"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 15V3m0 12l-4-4m4 4l4-4"/><path d="M2 17l.621 2.485A2 2 0 004.561 21h14.878a2 2 0 001.94-1.515L22 17"/></svg></div>
      <h3>Backup &amp; Sync</h3>
      <p>rsync, schedules, encryption, snapshots — backups between NAS devices over SSH.</p>
    </div>
    <div class="bento-item card-glow">
      <div class="bento-icon" style="color:#ef4444"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
      <h3>Surveillance</h3>
      <p>IP cameras, RTSP, motion recording, timeline — like Synology Surveillance without a license.</p>
    </div>
    <div class="bento-item card-glow">
      <div class="bento-icon" style="color:#8b5cf6"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="6" y="3" width="12" height="18" rx="2"/><path d="M12 18h.01"/></svg></div>
      <h3>Gallery</h3>
      <p>Thousands of photos with thumbnails, albums, sorting — your own Google Photos on your own hardware.</p>
    </div>
    <div class="bento-item bento-wide card-glow">
      <div class="bento-icon" style="color:#22c55e"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/></svg></div>
      <h3>Even more inside</h3>
      <p>SSH terminal, code editor, Docker manager, network printer, DDNS, AI chat, site builder, app store, and dozens of other tools.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header">
    <span class="section-tag">Philosophy</span>
    <h2>Why EthOS?</h2>
  </div>
  <div class="grid-3">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg></div>
      <h3>Privacy without compromise</h3>
      <p>Zero cloud, zero telemetry. Data physically never leaves your home. No one reads your files.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(139,92,246,.12);color:#8b5cf6"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M13 2L3 7l10 5 10-5-10-5z"/><path d="M3 17l10 5 10-5M3 12l10 5 10-5"/></svg></div>
      <h3>Modular like LEGO</h3>
      <p>Install only what you need. Package store — one click and you have a new feature.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(34,197,94,.12);color:#22c55e"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10A15.3 15.3 0 0112 2z"/></svg></div>
      <h3>No subscription</h3>
      <p>Free, open-source, forever. No hidden costs, subscriptions, or limits.</p>
    </div>
  </div>
</section>

<section class="cta-section">
  <div class="cta-glow"></div>
  <div class="cta-content">
    <h2>Ready for your own server?</h2>
    <p>5 minutes. Any computer with Linux. Zero experience needed.</p>
    <a href="installation.html" class="btn-primary btn-lg">Get started now <span class="btn-arrow">→</span></a>
  </div>
</section>'''
        },
        {
            'slug': 'features',
            'title': 'Features',
            'content': '''
<section class="page-hero">
  <span class="section-tag">Platform</span>
  <h1>Over 25 built-in<br><span class="gradient-text">applications</span></h1>
  <p class="hero-desc" style="max-width:560px;margin:0 auto;">A complete ecosystem for managing your home server — from files to camera surveillance.</p>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Files &amp; Storage</span><h2>Data Management</h2></div>
  <div class="grid-2">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg></div>
      <h3>File Manager</h3>
      <p>Browse, copy, move, share. Drag &amp; drop, thumbnail preview, built-in search. Supports multiple drives and partitions.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(34,197,94,.12);color:#22c55e"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 12v8a2 2 0 002 2h12a2 2 0 002-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></div>
      <h3>Network Sharing</h3>
      <p>Automatic SMB configuration — your folders visible on every device in your home network without any setup.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Data Protection</span><h2>Backups</h2></div>
  <div class="grid-2">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(245,158,11,.12);color:#f59e0b"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 15V3m0 12l-4-4m4 4l4-4"/><path d="M2 17l.621 2.485A2 2 0 004.561 21h14.878a2 2 0 001.94-1.515L22 17"/></svg></div>
      <h3>Backup Scheduler</h3>
      <p>rsync schedules — hourly, daily, weekly. Compression, encryption, retention. Backups between NAS devices over SSH.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(6,182,212,.12);color:#06b6d4"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></div>
      <h3>Snapshot Recovery</h3>
      <p>Browse snapshots from other NAS devices, restore individual files or entire folders with one click.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Media &amp; Entertainment</span><h2>Multimedia</h2></div>
  <div class="grid-2">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(139,92,246,.12);color:#8b5cf6"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></div>
      <h3>Photo Gallery</h3>
      <p>Automatic thumbnails, date sorting, albums. Smooth browsing of thousands of photos — your own Google Photos.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(236,72,153,.12);color:#ec4899"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></div>
      <h3>Download Manager</h3>
      <p>Torrents via Real-Debrid, direct links, YouTube. Built-in queue with priorities — everything to your NAS.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Security</span><h2>Surveillance &amp; Network</h2></div>
  <div class="grid-2">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(239,68,68,.12);color:#ef4444"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
      <h3>Surveillance Station</h3>
      <p>IP cameras (RTSP/ONVIF), live view, motion recording, timeline, clip export. Like Synology — for free.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(20,184,166,.12);color:#14b8a6"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
      <h3>Dynamic DNS &amp; Users</h3>
      <p>Remote access with DuckDNS/Cloudflare. Accounts with roles, app permissions, full access control.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Dev tools</span><h2>Tools</h2></div>
  <div class="grid-3">
    <div class="mini-card card-glow"><div class="mini-icon">⌨️</div><strong>SSH Terminal</strong><p>Full terminal in the browser</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">📝</div><strong>Code Editor</strong><p>Syntax highlighting, Git</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🐳</div><strong>Docker</strong><p>Container management</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🖨️</div><strong>Printer</strong><p>CUPS print server</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🤖</div><strong>AI Chat</strong><p>Built-in assistant</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🌐</div><strong>Site Builder</strong><p>Pages with CMS</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">📦</div><strong>AppStore</strong><p>Install packages</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🔄</div><strong>Updater</strong><p>OTA updates</p></div>
    <div class="mini-card card-glow"><div class="mini-icon">🔧</div><strong>Disk Repair</strong><p>fsck &amp; SMART</p></div>
  </div>
</section>'''
        },
        {
            'slug': 'installation',
            'title': 'Installation',
            'content': '''
<section class="page-hero">
  <span class="section-tag">Quick Start</span>
  <h1>Install in <span class="gradient-text">5 minutes</span></h1>
  <p class="hero-desc" style="max-width:480px;margin:0 auto;">Any computer with Linux. One command. Zero experience needed.</p>
</section>

<section class="section">
  <div class="install-steps">
    <div class="install-step card-glow">
      <div class="step-num">1</div>
      <div class="step-body">
        <h3>Open a terminal</h3>
        <p>SSH into your server or open a terminal locally on a Debian/Ubuntu machine.</p>
      </div>
    </div>
    <div class="install-step card-glow">
      <div class="step-num">2</div>
      <div class="step-body">
        <h3>Run the installer</h3>
        <pre>curl -fsSL https://get.ethos.local/install.sh | sudo bash</pre>
        <p>The installer will download dependencies, configure systemd, and start the server.</p>
      </div>
    </div>
    <div class="install-step card-glow">
      <div class="step-num">3</div>
      <div class="step-body">
        <h3>Open your browser</h3>
        <pre>http://&lt;SERVER-IP&gt;:9000</pre>
        <p>Set up an admin login and start using it — that's all.</p>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Hardware</span><h2>System Requirements</h2></div>
  <div class="grid-2">
    <div class="feature-card card-glow">
      <h3 style="color:var(--text-muted);text-transform:uppercase;font-size:.75em;letter-spacing:.1em;margin-bottom:12px;">Minimum</h3>
      <ul class="spec-list">
        <li><span class="spec-label">CPU</span> <span>x86_64 or ARM64</span></li>
        <li><span class="spec-label">RAM</span> <span>1 GB</span></li>
        <li><span class="spec-label">Disk</span> <span>8 GB</span></li>
        <li><span class="spec-label">OS</span> <span>Debian 11+ / Ubuntu 20.04+</span></li>
      </ul>
    </div>
    <div class="feature-card card-glow" style="border-color:var(--accent-glow);">
      <h3 style="color:var(--accent);text-transform:uppercase;font-size:.75em;letter-spacing:.1em;margin-bottom:12px;">⭐ Recommended</h3>
      <ul class="spec-list">
        <li><span class="spec-label">CPU</span> <span>Intel N100 / Ryzen</span></li>
        <li><span class="spec-label">RAM</span> <span>4 GB+</span></li>
        <li><span class="spec-label">Disk</span> <span>SSD 32 GB + HDD 1-4 TB</span></li>
        <li><span class="spec-label">Network</span> <span>Ethernet (1 Gbps)</span></li>
      </ul>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-header"><span class="section-tag">Recommendations</span><h2>What to run NAS on?</h2></div>
  <div class="grid-3">
    <div class="feature-card card-glow" style="text-align:center;">
      <div style="font-size:2.5em;margin-bottom:12px;filter:grayscale(0);">🍓</div>
      <h3>Raspberry Pi 4/5</h3>
      <p>Quiet, cheap, energy-efficient. Perfect to start — files, backup, printer.</p>
      <span class="price-tag">from ~$60</span>
    </div>
    <div class="feature-card card-glow" style="text-align:center;border-color:var(--accent-glow);">
      <div style="font-size:2.5em;margin-bottom:12px;">🖥️</div>
      <h3>Mini PC (N100)</h3>
      <p>Our favorite. Surveillance, Docker, transcoding — everything without a problem.</p>
      <span class="price-tag" style="background:var(--accent);color:#fff;">from ~$120 ⭐</span>
    </div>
    <div class="feature-card card-glow" style="text-align:center;">
      <div style="font-size:2.5em;margin-bottom:12px;">💻</div>
      <h3>Old Laptop/PC</h3>
      <p>Give a second life to old hardware. Debian + EthOS = ready NAS.</p>
      <span class="price-tag">Free</span>
    </div>
  </div>
</section>

<section class="cta-section">
  <div class="cta-glow"></div>
  <div class="cta-content">
    <h2>Need help?</h2>
    <p>Contact us — we are happy to help with installation and configuration.</p>
    <a href="contact.html" class="btn-primary btn-lg">Get in touch <span class="btn-arrow">→</span></a>
  </div>
</section>'''
        },
        {
            'slug': 'gallery',
            'title': 'Gallery',
            'content': '''
<section class="page-hero">
  <span class="section-tag">Preview</span>
  <h1>EthOS <span class="gradient-text">in action</span></h1>
  <p class="hero-desc" style="max-width:480px;margin:0 auto;">See what daily work with a home server looks like.</p>
</section>

<section class="section">
  <div class="gallery-grid">
    <div class="gallery-card card-glow">
      <div class="gallery-preview"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><rect x="4" y="4" width="40" height="32" rx="3"/><circle cx="14" cy="14" r="3"/><path d="M4 28l8-8 6 6 8-10 12 12"/></svg></div>
      <div class="gallery-info">
        <h3>Desktop</h3>
        <p>Desktop with app icons — like macOS, but in the browser. Multi-window, with drag &amp; drop.</p>
      </div>
    </div>
    <div class="gallery-card card-glow">
      <div class="gallery-preview" style="background:linear-gradient(135deg,rgba(139,92,246,.15),rgba(59,130,246,.15));"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg></div>
      <div class="gallery-info">
        <h3>File Manager</h3>
        <p>Folder browsing, preview, copying between drives — intuitive like a system explorer.</p>
      </div>
    </div>
    <div class="gallery-card card-glow">
      <div class="gallery-preview" style="background:linear-gradient(135deg,rgba(239,68,68,.15),rgba(245,158,11,.15));"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
      <div class="gallery-info">
        <h3>Surveillance Station</h3>
        <p>Live view, recording, timeline, clip export — full surveillance system without a license.</p>
      </div>
    </div>
    <div class="gallery-card card-glow">
      <div class="gallery-preview" style="background:linear-gradient(135deg,rgba(34,197,94,.15),rgba(6,182,212,.15));"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><path d="M12 15V3m0 12l-4-4m4 4l4-4M2 17l.621 2.485A2 2 0 004.561 21h14.878a2 2 0 001.94-1.515L22 17"/></svg></div>
      <div class="gallery-info">
        <h3>Backup Manager</h3>
        <p>Schedules, real-time progress, snapshots — automated backup management.</p>
      </div>
    </div>
    <div class="gallery-card card-glow">
      <div class="gallery-preview" style="background:linear-gradient(135deg,rgba(236,72,153,.15),rgba(139,92,246,.15));"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><rect x="6" y="3" width="12" height="18" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg></div>
      <div class="gallery-info">
        <h3>Printer &amp; AI</h3>
        <p>Print server with queue + built-in AI chatbot for questions and system configuration.</p>
      </div>
    </div>
    <div class="gallery-card card-glow">
      <div class="gallery-preview" style="background:linear-gradient(135deg,rgba(20,184,166,.15),rgba(59,130,246,.15));"><svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" opacity=".5"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/></svg></div>
      <div class="gallery-info">
        <h3>Terminal &amp; Docker</h3>
        <p>Full SSH in the browser + graphical Docker container manager without CLI.</p>
      </div>
    </div>
  </div>
</section>

<section class="cta-section">
  <div class="cta-glow"></div>
  <div class="cta-content">
    <h2>See for yourself</h2>
    <p>Install EthOS — 5 minutes and you have all of this on your own hardware.</p>
    <a href="installation.html" class="btn-primary btn-lg">Install now <span class="btn-arrow">→</span></a>
  </div>
</section>'''
        },
        {
            'slug': 'contact',
            'title': 'Contact',
            'content': '''
<section class="page-hero">
  <span class="section-tag">Contact</span>
  <h1>Let's talk</h1>
  <p class="hero-desc" style="max-width:420px;margin:0 auto;">Questions, suggestions, collaboration — feel free to write.</p>
</section>

<section class="section">
  <div class="grid-2">
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></div>
      <h3>Email</h3>
      <p style="font-size:1.1em;margin-top:4px;"><strong>contact@ethos.local</strong></p>
      <p style="color:var(--text-muted);font-size:.9em;">We reply within 24 hours.</p>
    </div>
    <div class="feature-card card-glow">
      <div class="feature-icon-wrap" style="background:rgba(139,92,246,.12);color:#8b5cf6"><svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg></div>
      <h3>Community</h3>
      <p style="font-size:1.1em;margin-top:4px;"><strong>forum.ethos.local</strong></p>
      <p style="color:var(--text-muted);font-size:.9em;">EthOS user forum.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="feature-card card-glow">
    <h3>Collaboration &amp; Open Source</h3>
    <p style="margin-bottom:16px;">EthOS is an open-source project. Every contribution counts:</p>
    <div class="contrib-grid">
      <div class="contrib-item"><span class="contrib-icon">🐛</span><div><strong>Report bugs</strong><p>Every report improves the system</p></div></div>
      <div class="contrib-item"><span class="contrib-icon">💡</span><div><strong>Suggest features</strong><p>Your ideas drive us forward</p></div></div>
      <div class="contrib-item"><span class="contrib-icon">📦</span><div><strong>Create packages</strong><p>Write apps for the AppStore</p></div></div>
      <div class="contrib-item"><span class="contrib-icon">📖</span><div><strong>Documentation</strong><p>Help others understand the system</p></div></div>
    </div>
  </div>
</section>

<section class="cta-section">
  <div class="cta-glow"></div>
  <div class="cta-content">
    <svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" style="opacity:.6;margin-bottom:12px;"><path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/></svg>
    <h2>Thank you for your interest</h2>
    <p>Built with passion for privacy, freedom, and simplicity.</p>
  </div>
</section>'''
        }
    ]

    site = {
        'id': site_id,
        'name': 'EthOS',
        'slug': 'ethos',
        'description': 'EthOS — private, free NAS system with 25+ built-in applications.',
        'template': 'landing',
        'theme': 'dark',
        'custom_css': '',
        'footer': f'&copy; {year} EthOS &middot; Open source &middot; Self-hosted &middot; Made with &#10084;&#65039;',
        'pages': pages,
        'created_at': now,
        'updated_at': now,
        'published_at': now,
        'published': True,
        'port': None
    }

    # Save & publish
    sites = [site]
    os.makedirs(_SITES_DIR, exist_ok=True)
    tmp = _SITES_META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _SITES_META)

    _publish_site(site)
    # Re-save with published_at
    tmp = _SITES_META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _SITES_META)

    log.info('Seeded EthOS promotional site')

def _load_sites():
    _ensure_dirs()
    try:
        with open(_SITES_META, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def _save_sites(sites):
    os.makedirs(_SITES_DIR, exist_ok=True)
    tmp = _SITES_META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _SITES_META)

def _find_site(site_id):
    sites = _load_sites()
    for s in sites:
        if s['id'] == site_id:
            return s, sites
    return None, sites

def _site_dir(site_id):
    return os.path.join(_SITES_DIR, site_id)

def _sanitize_slug(s):
    """Sanitize a string to a URL-friendly slug."""
    s = s.lower().strip()
    s = re.sub(r'[ąà]', 'a', s)
    s = re.sub(r'[ćč]', 'c', s)
    s = re.sub(r'[ęè]', 'e', s)
    s = re.sub(r'[łl]', 'l', s)
    s = re.sub(r'[ńñ]', 'n', s)
    s = re.sub(r'[óò]', 'o', s)
    s = re.sub(r'[śš]', 's', s)
    s = re.sub(r'[żźž]', 'z', s)
    s = re.sub(r'[^a-z0-9\-]', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s or 'page'


# ── Templates ──

_TEMPLATES = {
    'blank': {
        'name': 'Blank Page',
        'description': 'Completely empty page — start from scratch',
        'icon': 'fa-file',
        'color': '#64748b',
        'pages': [
            {'slug': 'index', 'title': 'Home Page', 'content': '<h1>My Website</h1>\n<p>Welcome to my website!</p>'}
        ]
    },
    'portfolio': {
        'name': 'Portfolio',
        'description': 'Portfolio page with "About Me", projects, and contact sections',
        'icon': 'fa-briefcase',
        'color': '#6366f1',
        'pages': [
            {'slug': 'index', 'title': 'Home Page', 'content': '''<section class="hero">
  <h1>John Smith</h1>
  <p class="subtitle">Web Developer & Designer</p>
  <p>I create modern websites and web applications.</p>
</section>

<section class="about" id="about">
  <h2>About Me</h2>
  <p>I am an experienced developer with a passion for creating beautiful and functional websites. I specialize in HTML, CSS, JavaScript, and modern frameworks.</p>
</section>

<section class="projects" id="projects">
  <h2>Projects</h2>
  <div class="grid-3">
    <div class="card">
      <h3>🌐 Business Website</h3>
      <p>Responsive website for a local business with a booking system.</p>
    </div>
    <div class="card">
      <h3>🛒 Online Store</h3>
      <p>E-commerce with payment integration and admin panel.</p>
    </div>
    <div class="card">
      <h3>📱 Mobile App</h3>
      <p>PWA for task management with cloud synchronization.</p>
    </div>
  </div>
</section>'''},
            {'slug': 'contact', 'title': 'Contact', 'content': '''<h1>Contact</h1>
<p>Want to collaborate? Write to me!</p>
<div class="contact-info">
  <p>📧 <strong>Email:</strong> john@example.com</p>
  <p>📱 <strong>Phone:</strong> +1 234 567 890</p>
  <p>🌍 <strong>Location:</strong> New York, USA</p>
</div>'''}
        ]
    },
    'blog': {
        'name': 'Blog',
        'description': 'Simple blog with articles and an "About Me" page',
        'icon': 'fa-pen-nib',
        'color': '#ec4899',
        'pages': [
            {'slug': 'index', 'title': 'Blog', 'content': '''<h1>My Blog</h1>
<p class="subtitle">Sharing knowledge and experience</p>

<article class="post">
  <h2>First blog post</h2>
  <p class="post-meta">📅 ''' + datetime.now().strftime('%d.%m.%Y') + ''' · ✍️ Admin</p>
  <p>Welcome to my blog! This is my first post. I will be sharing my thoughts on technology, programming, and life here.</p>
  <p>Feel free to read and comment!</p>
</article>

<article class="post">
  <h2>How to start your programming journey?</h2>
  <p class="post-meta">📅 ''' + datetime.now().strftime('%d.%m.%Y') + ''' · ✍️ Admin</p>
  <p>Programming is a fascinating skill that opens many doors. In this post, I will describe where to start learning and which technologies to choose.</p>
</article>'''},
            {'slug': 'about', 'title': 'About Me', 'content': '''<h1>About Me</h1>
<p>Hi! I am the author of this blog. I am interested in technology, programming, and sharing knowledge with others.</p>
<p>If you want to get in touch, write to: <strong>contact@example.com</strong></p>'''}
        ]
    },
    'landing': {
        'name': 'Landing page',
        'description': 'Single-page promotional site with CTA',
        'icon': 'fa-rocket',
        'color': '#f59e0b',
        'pages': [
            {'slug': 'index', 'title': 'Home Page', 'content': '''<section class="hero" style="text-align:center;padding:60px 20px;">
  <h1 style="font-size:2.5em;">Revolutionary Product</h1>
  <p class="subtitle" style="font-size:1.3em;opacity:.8;">It will change the way you work</p>
  <p style="margin:24px 0;">Join thousands of satisfied users and discover new possibilities.</p>
  <a href="#features" class="btn-primary" style="display:inline-block;padding:14px 32px;background:#3b82f6;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">Learn more</a>
</section>

<section id="features" style="padding:40px 20px;">
  <h2 style="text-align:center;">Why us?</h2>
  <div class="grid-3">
    <div class="card" style="text-align:center;">
      <div style="font-size:2.5em;">⚡</div>
      <h3>Speed</h3>
      <p>Lightning-fast performance thanks to the latest technologies.</p>
    </div>
    <div class="card" style="text-align:center;">
      <div style="font-size:2.5em;">🔒</div>
      <h3>Security</h3>
      <p>Your data is safe with end-to-end encryption.</p>
    </div>
    <div class="card" style="text-align:center;">
      <div style="font-size:2.5em;">🎯</div>
      <h3>Simplicity</h3>
      <p>Intuitive interface that you can master in minutes.</p>
    </div>
  </div>
</section>'''}
        ]
    }
}


# ── Built-in themes / CSS ──

_THEMES = {
    'light': {
        'name': 'Light',
        'css': '''
:root { --bg: #fafbfc; --bg-surface: #f0f2f5; --text: #111827; --text-muted: #6b7280;
  --accent: #3b82f6; --accent-hover: #2563eb; --accent-glow: rgba(59,130,246,.12);
  --card-bg: #ffffff; --card-border: rgba(0,0,0,.06); --card-shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 16px rgba(0,0,0,.04);
  --nav-bg: rgba(250,251,252,.88); --hero-bg: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  --glass: rgba(255,255,255,.75); --glass-border: rgba(0,0,0,.05); }
'''
    },
    'dark': {
        'name': 'Dark',
        'css': '''
:root { --bg: #0a0e1a; --bg-surface: #111827; --text: #f1f5f9; --text-muted: #94a3b8;
  --accent: #3b82f6; --accent-hover: #60a5fa; --accent-glow: rgba(59,130,246,.15);
  --card-bg: rgba(17,24,39,.7); --card-border: rgba(255,255,255,.06); --card-shadow: 0 1px 2px rgba(0,0,0,.2), 0 8px 24px rgba(0,0,0,.15);
  --nav-bg: rgba(10,14,26,.88); --hero-bg: linear-gradient(135deg, #1e3a5f 0%, #2d1b69 100%);
  --glass: rgba(17,24,39,.8); --glass-border: rgba(255,255,255,.06); }
'''
    },
    'ocean': {
        'name': 'Ocean',
        'css': '''
:root { --bg: #0c1929; --bg-surface: #0f2237; --text: #e0f2fe; --text-muted: #7dd3fc;
  --accent: #0ea5e9; --accent-hover: #38bdf8; --accent-glow: rgba(14,165,233,.15);
  --card-bg: rgba(15,34,55,.7); --card-border: rgba(125,211,252,.08); --card-shadow: 0 1px 2px rgba(0,0,0,.2), 0 8px 24px rgba(0,0,0,.15);
  --nav-bg: rgba(12,25,41,.88); --hero-bg: linear-gradient(135deg, #0369a1 0%, #0891b2 100%);
  --glass: rgba(15,34,55,.8); --glass-border: rgba(125,211,252,.06); }
'''
    },
    'forest': {
        'name': 'Forest',
        'css': '''
:root { --bg: #071a0e; --bg-surface: #0c2615; --text: #dcfce7; --text-muted: #86efac;
  --accent: #22c55e; --accent-hover: #4ade80; --accent-glow: rgba(34,197,94,.15);
  --card-bg: rgba(12,38,21,.7); --card-border: rgba(134,239,172,.08); --card-shadow: 0 1px 2px rgba(0,0,0,.2), 0 8px 24px rgba(0,0,0,.15);
  --nav-bg: rgba(7,26,14,.88); --hero-bg: linear-gradient(135deg, #166534 0%, #065f46 100%);
  --glass: rgba(12,38,21,.8); --glass-border: rgba(134,239,172,.06); }
'''
    }
}

# Shared base CSS appended to all themes — premium design
_BASE_CSS = '''
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.7; -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale; text-rendering: optimizeLegibility; }
.container { max-width: 960px; margin: 0 auto; padding: 0 28px; }
h1, h2, h3, h4 { margin-top: 0; font-weight: 700; letter-spacing: -0.025em; line-height: 1.2; }
h1 { font-size: 2.2em; }
h2 { font-size: 1.6em; margin-bottom: 12px; }
h3 { font-size: 1.1em; margin-bottom: 6px; }
p { margin-bottom: 12px; color: var(--text-muted); line-height: 1.7; }
h1 + p, h2 + p, h3 + p { margin-top: 2px; }
strong { color: var(--text); }
a { color: var(--accent); text-decoration: none; transition: color .2s; }
a:hover { color: var(--accent-hover); }
img { max-width: 100%; height: auto; border-radius: 12px; }
ul, ol { padding-left: 24px; color: var(--text-muted); }
li { margin-bottom: 4px; }
code { background: var(--card-bg); border: 1px solid var(--card-border); padding: 2px 7px; border-radius: 6px;
  font-size: .88em; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; }
pre { background: var(--bg-surface); border: 1px solid var(--card-border); padding: 20px; border-radius: 12px;
  overflow-x: auto; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: .88em;
  line-height: 1.6; user-select: all; color: var(--text); }

/* ─── Nav ─── */
.site-nav { position: sticky; top: 0; z-index: 100; background: var(--nav-bg); backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%); border-bottom: 1px solid var(--glass-border);
  padding: 0 32px; display: flex; align-items: center; gap: 0; height: 56px; }
.site-nav .site-title { font-weight: 800; font-size: 1em; color: var(--text); text-decoration: none; margin-right: 36px;
  display: flex; align-items: center; gap: 10px; letter-spacing: -0.02em; }
.site-nav .site-title::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); box-shadow: 0 0 12px var(--accent-glow); }
.site-nav a { color: var(--text-muted); text-decoration: none; font-size: .88em; font-weight: 500;
  padding: 18px 14px; transition: all .2s; border-bottom: 2px solid transparent; }
.site-nav a:hover { color: var(--text); }
.site-nav a.active-link { color: var(--accent); border-bottom-color: var(--accent); }

/* ─── Sections ─── */
.section { padding: 48px 0; }
.section-header { text-align: center; margin-bottom: 36px; }
.section-header h2 { font-size: 1.8em; font-weight: 800; letter-spacing: -0.03em; color: var(--text); }
.section-header p { font-size: 1.05em; max-width: 520px; margin: 0 auto; }
.section-tag { display: inline-block; font-size: .72em; font-weight: 600; text-transform: uppercase;
  letter-spacing: .12em; color: var(--accent); background: var(--accent-glow);
  padding: 4px 14px; border-radius: 100px; margin-bottom: 14px; }
.section-desc { color: var(--text-muted); font-size: 1.05em; }

/* ─── Hero Landing ─── */
.hero-landing { position: relative; text-align: center; padding: 100px 32px 80px; overflow: hidden; }
.hero-glow { position: absolute; top: -40%; left: 50%; transform: translateX(-50%); width: 800px; height: 600px;
  background: radial-gradient(ellipse, var(--accent-glow) 0%, transparent 70%); opacity: .6; pointer-events: none; }
.hero-grid-bg { position: absolute; inset: 0; opacity: .04;
  background-image: linear-gradient(var(--text-muted) 1px, transparent 1px),
    linear-gradient(90deg, var(--text-muted) 1px, transparent 1px);
  background-size: 60px 60px; pointer-events: none; mask-image: radial-gradient(ellipse at center, black 30%, transparent 70%); }
.hero-content { position: relative; z-index: 1; }
.hero-badge { display: inline-block; font-size: .75em; font-weight: 500; letter-spacing: .06em;
  color: var(--text-muted); border: 1px solid var(--card-border); background: var(--card-bg);
  padding: 6px 18px; border-radius: 100px; margin-bottom: 28px; backdrop-filter: blur(8px); }
.hero-title { font-size: 3.5em; font-weight: 900; letter-spacing: -0.04em; line-height: 1.1; margin-bottom: 20px; color: var(--text); }
.gradient-text { background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 50%, #ec4899 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
.hero-desc { font-size: 1.15em; color: var(--text-muted); line-height: 1.7; max-width: 540px; margin: 0 auto 32px; }
.hero-actions { display: flex; gap: 14px; justify-content: center; margin-bottom: 48px; flex-wrap: wrap; }
.hero-stats { display: flex; gap: 0; justify-content: center; align-items: center; }
.stat { text-align: center; padding: 0 28px; }
.stat-num { display: block; font-size: 1.5em; font-weight: 800; letter-spacing: -0.02em; color: var(--text); }
.stat-label { font-size: .78em; color: var(--text-muted); font-weight: 500; text-transform: uppercase; letter-spacing: .08em; }
.stat-sep { width: 1px; height: 32px; background: var(--card-border); }

/* ─── Page Hero ─── */
.page-hero { text-align: center; padding: 72px 32px 32px; }
.page-hero h1 { font-size: 2.8em; font-weight: 900; letter-spacing: -0.03em; margin-bottom: 12px; color: var(--text); }
.page-hero .hero-desc { margin-bottom: 0; }

/* ─── Buttons ─── */
.btn-primary { display: inline-flex; align-items: center; gap: 8px; padding: 13px 28px;
  background: var(--accent); color: #fff !important; border: none; border-radius: 12px;
  text-decoration: none !important; font-weight: 600; cursor: pointer; font-size: .92em;
  transition: all .25s; box-shadow: 0 1px 2px rgba(0,0,0,.1), 0 4px 16px var(--accent-glow);
  letter-spacing: -0.01em; }
.btn-primary:hover { background: var(--accent-hover); transform: translateY(-2px);
  box-shadow: 0 4px 20px var(--accent-glow), 0 8px 32px var(--accent-glow); }
.btn-primary .btn-arrow { transition: transform .2s; }
.btn-primary:hover .btn-arrow { transform: translateX(3px); }
.btn-lg { padding: 16px 36px; font-size: .98em; border-radius: 14px; }
.btn-ghost { display: inline-flex; align-items: center; gap: 6px; padding: 13px 28px; color: var(--text-muted) !important;
  border: 1px solid var(--card-border); border-radius: 12px; text-decoration: none !important;
  font-weight: 500; font-size: .92em; transition: all .25s; background: transparent; }
.btn-ghost:hover { color: var(--text) !important; border-color: var(--text-muted); background: var(--card-bg); }

/* ─── Bento Grid ─── */
.bento { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.bento-item { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px;
  padding: 28px; transition: all .3s; position: relative; overflow: hidden; }
.bento-item:hover { border-color: rgba(59,130,246,.2); transform: translateY(-2px); }
.bento-wide { grid-column: span 2; }
.bento-icon { width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;
  background: var(--accent-glow); color: var(--accent); border-radius: 12px; margin-bottom: 16px; }
.bento-item h3 { font-size: 1.05em; font-weight: 700; margin-bottom: 6px; color: var(--text); }
.bento-item p { font-size: .92em; margin-bottom: 0; }

/* ─── Feature Cards ─── */
.feature-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px;
  padding: 28px; transition: all .3s; }
.feature-card:hover { border-color: rgba(59,130,246,.15); transform: translateY(-2px); }
.feature-icon-wrap { width: 48px; height: 48px; display: flex; align-items: center; justify-content: center;
  background: var(--accent-glow); color: var(--accent); border-radius: 14px; margin-bottom: 18px; }
.feature-card h3 { font-size: 1.05em; font-weight: 700; margin-bottom: 6px; color: var(--text); }
.feature-card p { font-size: .92em; margin-bottom: 0; }

/* ─── Card Glow on Hover ─── */
.card-glow { position: relative; }
.card-glow::after { content: ""; position: absolute; inset: -1px; border-radius: inherit; opacity: 0;
  background: linear-gradient(135deg, rgba(59,130,246,.12), rgba(139,92,246,.08), transparent);
  transition: opacity .3s; pointer-events: none; z-index: 0; }
.card-glow:hover::after { opacity: 1; }
.card-glow > * { position: relative; z-index: 1; }

/* ─── Mini Card (tool grid) ─── */
.mini-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px;
  padding: 20px; text-align: center; transition: all .3s; }
.mini-card:hover { border-color: rgba(59,130,246,.15); transform: translateY(-2px); }
.mini-icon { font-size: 1.6em; margin-bottom: 8px; }
.mini-card strong { display: block; font-size: .92em; color: var(--text); margin-bottom: 4px; }
.mini-card p { font-size: .82em; margin: 0; }

/* ─── Gallery ─── */
.gallery-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.gallery-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px;
  overflow: hidden; transition: all .3s; }
.gallery-card:hover { border-color: rgba(59,130,246,.15); transform: translateY(-2px); }
.gallery-preview { height: 180px; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, rgba(59,130,246,.08), rgba(139,92,246,.08));
  border-bottom: 1px solid var(--card-border); color: var(--text-muted); }
.gallery-info { padding: 20px; }
.gallery-info h3 { font-size: 1em; font-weight: 700; margin-bottom: 4px; color: var(--text); }
.gallery-info p { font-size: .88em; margin: 0; }

/* ─── Install Steps ─── */
.install-steps { max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
.install-step { display: flex; gap: 20px; background: var(--card-bg); border: 1px solid var(--card-border);
  border-radius: 16px; padding: 24px; align-items: flex-start; transition: all .3s; }
.install-step:hover { border-color: rgba(59,130,246,.15); }
.step-num { flex-shrink: 0; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
  background: var(--accent-glow); color: var(--accent); font-weight: 800; font-size: 1em;
  border-radius: 12px; border: 1px solid rgba(59,130,246,.2); }
.step-body { flex: 1; min-width: 0; }
.step-body h3 { font-size: 1em; color: var(--text); margin-bottom: 6px; }
.step-body p { font-size: .9em; margin-bottom: 0; }
.step-body pre { margin-top: 10px; font-size: .85em; padding: 14px; }

/* ─── Spec List ─── */
.spec-list { list-style: none; padding: 0; }
.spec-list li { display: flex; justify-content: space-between; padding: 8px 0;
  border-bottom: 1px solid var(--card-border); font-size: .92em; color: var(--text-muted); }
.spec-list li:last-child { border-bottom: none; }
.spec-label { font-weight: 600; color: var(--text); font-size: .82em; text-transform: uppercase;
  letter-spacing: .06em; min-width: 60px; }

/* ─── Price Tag ─── */
.price-tag { display: inline-block; font-size: .82em; font-weight: 600; padding: 5px 14px;
  border-radius: 100px; background: var(--card-border); color: var(--text-muted); margin-top: 12px; }

/* ─── Contrib Grid ─── */
.contrib-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
.contrib-item { display: flex; gap: 12px; align-items: flex-start; padding: 12px; border-radius: 12px;
  background: rgba(255,255,255,.02); border: 1px solid var(--card-border); }
.contrib-icon { font-size: 1.3em; flex-shrink: 0; margin-top: 2px; }
.contrib-item strong { display: block; font-size: .9em; color: var(--text); }
.contrib-item p { font-size: .82em; margin: 2px 0 0; }

/* ─── CTA Section ─── */
.cta-section { position: relative; text-align: center; margin: 48px 0; padding: 64px 32px;
  background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 20px; overflow: hidden; }
.cta-glow { position: absolute; top: -50%; left: 50%; transform: translateX(-50%); width: 600px; height: 400px;
  background: radial-gradient(ellipse, var(--accent-glow) 0%, transparent 70%); opacity: .5; pointer-events: none; }
.cta-content { position: relative; z-index: 1; }
.cta-content h2 { font-size: 1.8em; font-weight: 800; letter-spacing: -0.03em; color: var(--text); }
.cta-content p { font-size: 1.05em; margin: 10px 0 24px; }

/* ─── Grid helpers ─── */
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }

/* ─── Classic card (templates) ─── */
.card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; padding: 24px;
  box-shadow: var(--card-shadow); transition: all .3s; }
.card:hover { border-color: rgba(59,130,246,.15); transform: translateY(-2px); }

/* ─── Posts (blog template) ─── */
.post { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; padding: 28px; margin: 18px 0;
  box-shadow: var(--card-shadow); }
.post h2 { margin-bottom: 6px; }
.post-meta { font-size: .85em; color: var(--text-muted); margin-bottom: 14px; }

/* ─── Classic Hero (templates) ─── */
.hero { background: var(--hero-bg); color: #fff; padding: 72px 32px; text-align: center; border-radius: 18px; margin: 28px 0;
  position: relative; overflow: hidden; }
.hero::before { content: ""; position: absolute; inset: 0; background:
  radial-gradient(ellipse at 20% 50%, rgba(59,130,246,.15) 0%, transparent 50%),
  radial-gradient(ellipse at 80% 20%, rgba(139,92,246,.1) 0%, transparent 50%); pointer-events: none; }
.hero > * { position: relative; }
.hero h1 { font-size: 2.8em; margin-bottom: 10px; font-weight: 800; letter-spacing: -0.03em; }
.subtitle { font-size: 1.15em; opacity: .85; font-weight: 400; }

/* ─── Contact (template) ─── */
.contact-info { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px;
  padding: 28px; margin-top: 18px; box-shadow: var(--card-shadow); }
.contact-info p { margin: 10px 0; }

/* ─── Footer ─── */
.site-footer { text-align: center; padding: 32px 24px; margin-top: 48px; font-size: .82em; color: var(--text-muted);
  border-top: 1px solid var(--card-border); letter-spacing: .01em; }

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .hero-landing { padding: 72px 20px 56px; }
  .hero-title { font-size: 2.2em !important; }
  .hero-desc { font-size: 1em; br { display: none; } }
  .hero-stats { flex-direction: column; gap: 12px; }
  .stat-sep { width: 40px; height: 1px; }
  .stat { padding: 6px 0; }
  .bento, .gallery-grid { grid-template-columns: 1fr; }
  .bento-wide { grid-column: span 1; }
  .grid-3, .grid-2 { grid-template-columns: 1fr; }
  .site-nav { padding: 0 16px; height: 48px; overflow-x: auto; }
  .site-nav a { padding: 14px 10px; font-size: .84em; white-space: nowrap; }
  .container { padding: 0 18px; }
  .page-hero h1 { font-size: 2em; }
  .section { padding: 32px 0; }
  .section-header h2 { font-size: 1.4em; }
  .cta-section { padding: 48px 20px; }
  .contrib-grid { grid-template-columns: 1fr; }
  .install-step { flex-direction: column; gap: 12px; }
}

/* ─── Scrollbar ─── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ─── Selection ─── */
::selection { background: var(--accent-glow); color: var(--accent); }
'''


def _build_site_html(site, page, base_url=None):
    """Build a full HTML page for serving.
    base_url: if set, all nav links are prefixed (e.g. /api/websites/id/preview)
    """
    theme_css = _THEMES.get(site.get('theme', 'light'), _THEMES['light'])['css']
    custom_css = site.get('custom_css', '')
    prefix = (base_url.rstrip('/') + '/') if base_url else ''

    # Build nav links
    pages = site.get('pages', [])
    nav_links = ''
    for p in pages:
        active = ' class="active-link"' if p['slug'] == page['slug'] else ''
        href = f"{prefix}index.html" if p['slug'] == 'index' else f"{prefix}{p['slug']}.html"
        nav_links += f'<a href="{href}"{active}>{p["title"]}</a>\n'

    site_title = site.get('name', 'My Website')
    page_title = page.get('title', site_title)
    footer = site.get('footer', f'&copy; {datetime.now().year} {site_title}')
    home_href = f"{prefix}index.html"
    base_tag = f'<base href="{prefix}">' if base_url else ''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {base_tag}
    <title>{page_title} — {site_title}</title>
    <meta name="description" content="{site.get('description', '')}">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🖥️</text></svg>">
    <style>{theme_css}{_BASE_CSS}{custom_css}</style>
</head>
<body>
    <nav class="site-nav">
        <a href="{home_href}" class="site-title">{site_title}</a>
        {nav_links}
    </nav>
    <main class="container" style="padding-top:28px;padding-bottom:48px;">
        {page.get('content', '')}
    </main>
    <footer class="site-footer">{footer}</footer>
</body>
</html>'''


def _publish_site(site):
    """Write all HTML files for a site to its directory."""
    sd = _site_dir(site['id'])
    os.makedirs(sd, exist_ok=True)

    for page in site.get('pages', []):
        fname = 'index.html' if page['slug'] == 'index' else f"{page['slug']}.html"
        html = _build_site_html(site, page)
        fpath = os.path.join(sd, fname)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(html)

    site['published_at'] = datetime.now().isoformat()
    return True


# ══════════════════════════════════════════════════════
# ══  API Routes
# ══════════════════════════════════════════════════════

@websites_bp.route('/templates')
def list_templates():
    """List available site templates."""
    tpls = []
    for tid, t in _TEMPLATES.items():
        tpls.append({
            'id': tid,
            'name': t['name'],
            'description': t['description'],
            'icon': t['icon'],
            'color': t['color'],
            'page_count': len(t['pages'])
        })
    return jsonify({'templates': tpls})


@websites_bp.route('/themes')
def list_themes():
    """List available themes."""
    themes = []
    for tid, t in _THEMES.items():
        themes.append({'id': tid, 'name': t['name']})
    return jsonify({'themes': themes})


@websites_bp.route('/')
def list_sites():
    """List all sites."""
    sites = _load_sites()
    return jsonify({'sites': sites})


@websites_bp.route('/', methods=['POST'])
def create_site():
    """Create a new site from a template."""
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '').strip()
    template_id = data.get('template', 'blank')
    theme = data.get('theme', 'light')
    description = data.get('description', '').strip()

    if not name:
        return jsonify({'error': 'Site name is required'}), 400

    template = _TEMPLATES.get(template_id)
    if not template:
        return jsonify({'error': 'Unknown template'}), 400

    site_id = str(uuid.uuid4())[:8]
    slug = _sanitize_slug(name)

    # Copy pages from template
    pages = []
    for p in template['pages']:
        pages.append({
            'slug': p['slug'],
            'title': p['title'],
            'content': p['content']
        })

    site = {
        'id': site_id,
        'name': name,
        'slug': slug,
        'description': description,
        'template': template_id,
        'theme': theme,
        'custom_css': '',
        'footer': f'© {datetime.now().year} {name}',
        'pages': pages,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'published_at': None,
        'published': False,
        'port': None
    }

    # Publish initial files
    _publish_site(site)
    site['published'] = True

    sites = _load_sites()
    sites.append(site)
    _save_sites(sites)

    log.info('Created site "%s" (id=%s, template=%s)', name, site_id, template_id)
    return jsonify({'site': site})


@websites_bp.route('/<site_id>')
def get_site(site_id):
    """Get site details including pages."""
    site, _ = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404
    return jsonify({'site': site})


@websites_bp.route('/<site_id>', methods=['PUT'])
def update_site(site_id):
    """Update site settings (name, theme, description, footer, custom_css)."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    for key in ('name', 'description', 'theme', 'footer', 'custom_css'):
        if key in data:
            site[key] = data[key].strip() if isinstance(data[key], str) else data[key]

    if 'name' in data:
        site['slug'] = _sanitize_slug(data['name'])

    site['updated_at'] = datetime.now().isoformat()

    # Re-publish
    _publish_site(site)
    _save_sites(sites)

    return jsonify({'site': site})


@websites_bp.route('/<site_id>', methods=['DELETE'])
def delete_site(site_id):
    """Delete a site and all its files."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    # Remove files
    sd = _site_dir(site_id)
    if os.path.isdir(sd):
        shutil.rmtree(sd, ignore_errors=True)

    sites = [s for s in sites if s['id'] != site_id]
    _save_sites(sites)

    log.info('Deleted site "%s" (id=%s)', site.get('name', '?'), site_id)
    return jsonify({'ok': True})


# ── Pages ──

@websites_bp.route('/<site_id>/pages', methods=['POST'])
def create_page(site_id):
    """Add a new page to a site."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'Page title is required'}), 400

    slug = _sanitize_slug(title)
    # Ensure unique slug
    existing_slugs = {p['slug'] for p in site.get('pages', [])}
    base_slug = slug
    counter = 1
    while slug in existing_slugs:
        slug = f'{base_slug}-{counter}'
        counter += 1

    page = {
        'slug': slug,
        'title': title,
        'content': data.get('content', f'<h1>{title}</h1>\n<p>Page content...</p>')
    }

    site.setdefault('pages', []).append(page)
    site['updated_at'] = datetime.now().isoformat()
    _publish_site(site)
    _save_sites(sites)

    return jsonify({'page': page, 'site': site})


@websites_bp.route('/<site_id>/pages/<slug>', methods=['PUT'])
def update_page(site_id, slug):
    """Update a page's title and/or content."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    page = None
    for p in site.get('pages', []):
        if p['slug'] == slug:
            page = p
            break

    if not page:
        return jsonify({'error': 'Subpage not found'}), 404

    data = request.get_json(force=True, silent=True) or {}
    if 'title' in data:
        page['title'] = data['title'].strip()
    if 'content' in data:
        page['content'] = data['content']

    site['updated_at'] = datetime.now().isoformat()
    _publish_site(site)
    _save_sites(sites)

    return jsonify({'page': page, 'site': site})


@websites_bp.route('/<site_id>/pages/<slug>', methods=['DELETE'])
def delete_page(site_id, slug):
    """Delete a page from a site."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    if slug == 'index':
        return jsonify({'error': 'Cannot delete the home page'}), 400

    site['pages'] = [p for p in site.get('pages', []) if p['slug'] != slug]
    site['updated_at'] = datetime.now().isoformat()

    # Remove file
    fpath = os.path.join(_site_dir(site_id), f'{slug}.html')
    if os.path.isfile(fpath):
        os.remove(fpath)

    _publish_site(site)
    _save_sites(sites)

    return jsonify({'ok': True, 'site': site})


# ── Preview / Serve ──

@websites_bp.route('/<site_id>/preview')
@websites_bp.route('/<site_id>/preview/<path:page_path>')
def preview_site(site_id, page_path='index.html'):
    """Serve a site page for preview — rendered dynamically with correct links."""
    site, _ = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    if not page_path.endswith('.html'):
        page_path += '.html'

    # Find the page by slug
    slug = page_path.replace('.html', '')
    page = None
    for p in site.get('pages', []):
        if p['slug'] == slug:
            page = p
            break
    if not page:
        # fallback to index
        page = next((p for p in site.get('pages', []) if p['slug'] == 'index'), None)
        if not page:
            return jsonify({'error': 'Site not found'}), 404

    base_url = f'/api/websites/{site_id}/preview'
    html = _build_site_html(site, page, base_url=base_url)
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ── Publish toggle ──

@websites_bp.route('/<site_id>/publish', methods=['POST'])
def publish_site(site_id):
    """Re-publish a site (regenerate all HTML)."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    _publish_site(site)
    site['published'] = True
    _save_sites(sites)

    return jsonify({'site': site})


@websites_bp.route('/<site_id>/unpublish', methods=['POST'])
def unpublish_site(site_id):
    """Mark site as unpublished."""
    site, sites = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    site['published'] = False
    _save_sites(sites)

    return jsonify({'site': site})


# ── Export ──

@websites_bp.route('/<site_id>/export')
def export_site(site_id):
    """Return a ZIP archive of the site's files."""
    import zipfile
    import io

    site, _ = _find_site(site_id)
    if not site:
        return jsonify({'error': 'Site not found'}), 404

    sd = _site_dir(site_id)
    if not os.path.isdir(sd):
        return jsonify({'error': 'No site files'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(sd):
            for fn in files:
                fp = os.path.join(root, fn)
                arcname = os.path.relpath(fp, sd)
                zf.write(fp, arcname)

    buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True,
                     download_name=f'{site.get("slug", site_id)}.zip')


# ── Package install / uninstall ──
register_pkg_routes(
    websites_bp,
    install_message='Websites ready.',
    wipe_dirs=[_DATA_DIR],
)
