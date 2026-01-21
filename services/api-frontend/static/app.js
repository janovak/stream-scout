// StreamScout Frontend Application

const API_BASE = '/v1.0';

// Intensity levels configuration (FR-001a: stored as frontend constant)
const INTENSITY_LEVELS = [
    { threshold: 3, label: "Warming Up" },
    { threshold: 5, label: "Getting Good" },
    { threshold: 7, label: "Popping Off" },
    { threshold: 9, label: "Unhinged" },
    { threshold: 11, label: "Legendary" }
];

// State
let clips = [];
let selectedThreshold = 5; // Default: "Getting Good" (FR-002b)
let totalCount = 0;
let offset = 0;
let hasMore = false;
let isLoading = false;
let playingClipId = null;

// DOM Elements
const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error');
const noClipsEl = document.getElementById('no-clips');
const clipsGridEl = document.getElementById('clips-grid');
const intensityFilterEl = document.getElementById('intensity-filter');
const clipCountEl = document.getElementById('clip-count');

// Infinite scroll configuration
const SCROLL_THRESHOLD = 300; // pixels from bottom to trigger load
let scrollTimeout = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    initializeFilter();
    loadClips();
    initializeInfiniteScroll();
});

// Stop playing clip on escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        stopCurrentClip();
    }
});

function initializeFilter() {
    // Populate filter dropdown with intensity levels
    intensityFilterEl.innerHTML = INTENSITY_LEVELS.map(level =>
        `<option value="${level.threshold}" ${level.threshold === selectedThreshold ? 'selected' : ''}>
            ${level.label} (${level.threshold}+ σ)
        </option>`
    ).join('');
}

function onFilterChange() {
    selectedThreshold = parseInt(intensityFilterEl.value);
    offset = 0; // Reset pagination on filter change
    stopCurrentClip(); // Stop any playing clip
    clips = []; // Clear existing clips
    playingClipId = null; // Reset playing state
    hideBottomLoader(); // Remove any loading indicator
    loadClips();
}

async function loadClips() {
    if (isLoading) return;
    isLoading = true;

    if (offset === 0) {
        showLoading();
    }

    try {
        const url = `${API_BASE}/clip?min_intensity=${selectedThreshold}&limit=24&offset=${offset}`;
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        if (offset === 0) {
            clips = data.clips || [];
        } else {
            clips = [...clips, ...(data.clips || [])];
        }

        totalCount = data.total_count || 0;
        hasMore = data.has_more || false;

        updateClipCount();

        if (clips.length === 0) {
            showNoClips();
        } else {
            renderClips();
        }
    } catch (error) {
        console.error('Failed to load clips:', error);
        showError();
    } finally {
        isLoading = false;
    }
}

function updateClipCount() {
    const level = INTENSITY_LEVELS.find(l => l.threshold === selectedThreshold);
    const levelName = level ? level.label : `${selectedThreshold}+ σ`;
    clipCountEl.textContent = `${totalCount} clip${totalCount !== 1 ? 's' : ''} at "${levelName}" or higher`;
}

function showLoading() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');
    // Show skeleton loading cards
    clipsGridEl.innerHTML = generateSkeletonCards(12);
}

function generateSkeletonCards(count) {
    return Array(count).fill(null).map(() => `
        <div class="skeleton-card">
            <div class="skeleton-thumbnail"></div>
            <div class="skeleton-meta">
                <div class="skeleton-title"></div>
                <div class="skeleton-details">
                    <div class="skeleton-badge"></div>
                    <div class="skeleton-time"></div>
                </div>
            </div>
        </div>
    `).join('');
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

function getIntensityLabel(intensity) {
    if (intensity === null || intensity === undefined) {
        return { label: "Unknown", threshold: 0 };
    }
    // Find the highest threshold that the intensity meets
    for (let i = INTENSITY_LEVELS.length - 1; i >= 0; i--) {
        if (intensity >= INTENSITY_LEVELS[i].threshold) {
            return INTENSITY_LEVELS[i];
        }
    }
    return { label: "Low", threshold: 0 };
}

function renderClips() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');

    clipsGridEl.innerHTML = clips.map((clip, index) => {
        const intensityInfo = getIntensityLabel(clip.intensity);
        const intensityDisplay = clip.intensity !== null
            ? `${intensityInfo.label} (${clip.intensity.toFixed(1)}σ)`
            : 'Score unavailable';
        const isPlaying = playingClipId === clip.clip_id;

        return `
        <div class="clip-card${isPlaying ? ' playing' : ''}" data-clip-id="${escapeHtml(clip.clip_id)}" onclick="openClip(${index})">
            <div class="clip-thumbnail">
                ${isPlaying ? `
                    <div class="inline-player">
                        <iframe src="${escapeHtml(clip.embed_url)}&parent=${window.location.hostname}&autoplay=true"
                                allowfullscreen
                                allow="autoplay; encrypted-media"></iframe>
                    </div>
                ` : `
                    <img src="${escapeHtml(clip.thumbnail_url)}"
                         alt="Clip from ${escapeHtml(clip.streamer_login || 'Unknown')}"
                         onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 9%22><rect fill=%22%231f1f23%22 width=%2216%22 height=%229%22/></svg>'">
                    <div class="play-overlay">
                        <div class="play-icon"></div>
                    </div>
                `}
            </div>
            <div class="clip-meta">
                <h3>${escapeHtml(clip.streamer_login || 'Unknown Streamer')}</h3>
                <div class="clip-details">
                    <span class="intensity-badge" data-level="${intensityInfo.threshold}">${intensityDisplay}</span>
                    <span class="clip-time">${formatTime(clip.detected_at)}</span>
                </div>
            </div>
        </div>
    `}).join('');
}

function openClip(index) {
    const clip = clips[index];
    if (!clip || !clip.embed_url) return;

    // If clicking the same clip that's already playing, do nothing
    if (playingClipId === clip.clip_id) return;

    // Stop any currently playing clip
    stopCurrentClip();

    // Mark this clip as playing
    playingClipId = clip.clip_id;

    // Find the clip card and replace thumbnail with player
    const clipCard = document.querySelector(`[data-clip-id="${clip.clip_id}"]`);
    if (!clipCard) return;

    const thumbnailContainer = clipCard.querySelector('.clip-thumbnail');
    if (!thumbnailContainer) return;

    // Create Twitch embed player with autoplay
    const embedUrl = clip.embed_url + '&parent=' + window.location.hostname + '&autoplay=true';
    thumbnailContainer.innerHTML = `
        <div class="inline-player">
            <iframe src="${escapeHtml(embedUrl)}"
                    allowfullscreen
                    allow="autoplay; encrypted-media"></iframe>
        </div>
    `;
    clipCard.classList.add('playing');
}

function stopCurrentClip() {
    if (!playingClipId) return;

    // Find the currently playing clip card
    const playingCard = document.querySelector(`[data-clip-id="${playingClipId}"]`);
    if (playingCard) {
        // Restore the thumbnail
        const clip = clips.find(c => c.clip_id === playingClipId);
        if (clip) {
            const thumbnailContainer = playingCard.querySelector('.clip-thumbnail');
            if (thumbnailContainer) {
                thumbnailContainer.innerHTML = `
                    <img src="${escapeHtml(clip.thumbnail_url)}"
                         alt="Clip from ${escapeHtml(clip.streamer_login || 'Unknown')}"
                         onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 9%22><rect fill=%22%231f1f23%22 width=%2216%22 height=%229%22/></svg>'">
                    <div class="play-overlay">
                        <div class="play-icon"></div>
                    </div>
                `;
            }
        }
        playingCard.classList.remove('playing');
    }

    playingClipId = null;
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

// Infinite Scroll Functions
function initializeInfiniteScroll() {
    window.addEventListener('scroll', handleScroll);
}

function handleScroll() {
    // Debounce scroll events
    if (scrollTimeout) {
        clearTimeout(scrollTimeout);
    }

    scrollTimeout = setTimeout(() => {
        const scrollPosition = window.innerHeight + window.scrollY;
        const documentHeight = document.documentElement.scrollHeight;

        // Check if near bottom of page
        if (documentHeight - scrollPosition < SCROLL_THRESHOLD) {
            loadMoreClips();
        }
    }, 100);
}

function loadMoreClips() {
    // Don't load if already loading, no more clips, or no clips loaded yet
    if (isLoading || !hasMore || clips.length === 0) return;

    // Update offset for next page
    offset = clips.length;

    // Show loading indicator at bottom
    showBottomLoader();

    // Load next batch
    loadClipsAppend();
}

async function loadClipsAppend() {
    if (isLoading) return;
    isLoading = true;

    try {
        const url = `${API_BASE}/clip?min_intensity=${selectedThreshold}&limit=24&offset=${offset}`;
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        const newClips = data.clips || [];

        // Append new clips to array
        clips = [...clips, ...newClips];
        totalCount = data.total_count || 0;
        hasMore = data.has_more || false;

        updateClipCount();

        // Append new clips to grid (don't re-render everything)
        appendClipsToGrid(newClips);

        // Update or remove bottom loader
        hideBottomLoader();

    } catch (error) {
        console.error('Failed to load more clips:', error);
        hideBottomLoader();
    } finally {
        isLoading = false;
    }
}

function appendClipsToGrid(newClips) {
    const startIndex = clips.length - newClips.length;

    const newCardsHtml = newClips.map((clip, i) => {
        const index = startIndex + i;
        const intensityInfo = getIntensityLabel(clip.intensity);
        const intensityDisplay = clip.intensity !== null
            ? `${intensityInfo.label} (${clip.intensity.toFixed(1)}σ)`
            : 'Score unavailable';

        return `
        <div class="clip-card" data-clip-id="${escapeHtml(clip.clip_id)}" onclick="openClip(${index})">
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
                <div class="clip-details">
                    <span class="intensity-badge" data-level="${intensityInfo.threshold}">${intensityDisplay}</span>
                    <span class="clip-time">${formatTime(clip.detected_at)}</span>
                </div>
            </div>
        </div>
    `}).join('');

    // Insert before the loader if it exists, otherwise append
    const loader = document.getElementById('bottom-loader');
    if (loader) {
        loader.insertAdjacentHTML('beforebegin', newCardsHtml);
    } else {
        clipsGridEl.insertAdjacentHTML('beforeend', newCardsHtml);
    }
}

function showBottomLoader() {
    // Remove existing loader if any
    hideBottomLoader();

    // Add loader element after the grid
    const loaderHtml = `
        <div id="bottom-loader" class="bottom-loader">
            <div class="spinner"></div>
            <p>Loading more clips...</p>
        </div>
    `;
    clipsGridEl.insertAdjacentHTML('afterend', loaderHtml);
}

function hideBottomLoader() {
    const loader = document.getElementById('bottom-loader');
    if (loader) {
        // If no more clips, show end message briefly
        if (!hasMore && clips.length > 0) {
            loader.innerHTML = '<p class="end-message">No more clips to load</p>';
            setTimeout(() => {
                loader.remove();
            }, 2000);
        } else {
            loader.remove();
        }
    }
}
