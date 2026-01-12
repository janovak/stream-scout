// StreamScout Frontend Application

const API_BASE = '/v1.0';
let clips = [];

// DOM Elements
const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error');
const noClipsEl = document.getElementById('no-clips');
const clipsGridEl = document.getElementById('clips-grid');
const modalEl = document.getElementById('clip-modal');
const clipPlayerEl = document.getElementById('clip-player');
const clipStreamerEl = document.getElementById('clip-streamer');
const clipTimeEl = document.getElementById('clip-time');

// Load clips on page load
document.addEventListener('DOMContentLoaded', loadClips);

// Close modal on escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
    }
});

// Close modal on backdrop click
modalEl.addEventListener('click', (e) => {
    if (e.target === modalEl) {
        closeModal();
    }
});

async function loadClips() {
    showLoading();

    try {
        const response = await fetch(`${API_BASE}/clip`);

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        clips = data.clips || [];

        if (clips.length === 0) {
            showNoClips();
        } else {
            renderClips();
        }
    } catch (error) {
        console.error('Failed to load clips:', error);
        showError();
    }
}

function showLoading() {
    loadingEl.classList.remove('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');
    clipsGridEl.innerHTML = '';
}

function showError() {
    loadingEl.classList.add('hidden');
    errorEl.classList.remove('hidden');
    noClipsEl.classList.add('hidden');
}

function showNoClips() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.remove('hidden');
}

function renderClips() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');

    clipsGridEl.innerHTML = clips.map((clip, index) => `
        <div class="clip-card" onclick="openClip(${index})">
            <div class="clip-thumbnail">
                <img src="${escapeHtml(clip.thumbnail_url)}"
                     alt="Clip from ${escapeHtml(clip.streamer_login || 'Unknown')}"
                     onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 9%22><rect fill=%22%231f1f23%22 width=%2216%22 height=%229%22/></svg>'">
                <div class="play-overlay">
                    <div class="play-icon"></div>
                </div>
            </div>
            <div class="clip-meta">
                <h3>${escapeHtml(clip.streamer_login || 'Unknown Streamer')}</h3>
                <p>${formatTime(clip.detected_at)}</p>
            </div>
        </div>
    `).join('');
}

function openClip(index) {
    const clip = clips[index];
    if (!clip || !clip.embed_url) return;

    // Create Twitch embed player
    const embedUrl = clip.embed_url + '&parent=' + window.location.hostname + '&autoplay=true';
    clipPlayerEl.innerHTML = `<iframe src="${escapeHtml(embedUrl)}" allowfullscreen></iframe>`;

    // Update clip info
    clipStreamerEl.textContent = clip.streamer_login || 'Unknown Streamer';
    clipTimeEl.textContent = formatTime(clip.detected_at);

    // Show modal
    modalEl.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closeModal() {
    modalEl.classList.add('hidden');
    clipPlayerEl.innerHTML = '';
    document.body.style.overflow = '';
}

function formatTime(isoString) {
    if (!isoString) return 'Unknown time';

    try {
        const date = new Date(isoString);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins} minute${diffMins !== 1 ? 's' : ''} ago`;
        if (diffHours < 24) return `${diffHours} hour${diffHours !== 1 ? 's' : ''} ago`;
        if (diffDays < 7) return `${diffDays} day${diffDays !== 1 ? 's' : ''} ago`;

        return date.toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    } catch {
        return 'Unknown time';
    }
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
