// StreamScout Frontend Application

// Intensity levels configuration (FR-001a: stored as frontend constant)
const INTENSITY_LEVELS = [
    { threshold: 7, label: "Popping Off" },
    { threshold: 9, label: "Unhinged" },
    { threshold: 11, label: "Legendary" }
];

// State
let clips = [];
// Must match DEFAULT_MIN_INTENSITY in api_frontend_service.py (spec 002 FR-002b).
let selectedThreshold = 9; // Default: "Unhinged" (FR-002b)
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
    intensityFilterEl.innerHTML = INTENSITY_LEVELS.map(level =>
        `<option value="${level.threshold}" ${level.threshold === selectedThreshold ? 'selected' : ''}>
            ${level.label}
        </option>`
    ).join('');
}

function onFilterChange() {
    selectedThreshold = parseInt(intensityFilterEl.value);
    stopCurrentClip();
    clips = [];
    playingClipId = null;
    ClipFeed.reset();
    hideBottomLoader();
    loadClips();
}

// One loader for both the initial/filter-change load (replace = true) and
// the infinite-scroll append (replace = false).
async function loadClips(replace = true) {
    if (isLoading) return;
    isLoading = true;

    if (replace) {
        offset = 0;
        showLoading();
    } else {
        showBottomLoader();
    }

    try {
        const result = await ClipFeed.page(selectedThreshold, offset);
        applyPage(result, replace);
    } catch (error) {
        console.error('Failed to load clips:', error);
        if (replace) {
            showError();
        } else {
            hideBottomLoader();
        }
    } finally {
        isLoading = false;
    }
}

function applyPage(result, replace) {
    clips = replace ? result.clips : [...clips, ...result.clips];
    totalCount = result.totalCount;
    hasMore = result.hasMore;
    offset = clips.length;

    updateClipCount();

    if (replace) {
        if (clips.length === 0) {
            showNoClips();
        } else {
            renderClips();
        }
    } else {
        appendClipsToGrid(result.clips);
        hideBottomLoader();
    }

    if (hasMore) {
        ClipFeed.prefetch(selectedThreshold, clips.length);
    }
}

function updateClipCount() {
    const level = INTENSITY_LEVELS.find(l => l.threshold === selectedThreshold);
    const levelName = level ? level.label : selectedThreshold;
    const isHighest = level && level.threshold === INTENSITY_LEVELS[INTENSITY_LEVELS.length - 1].threshold;
    const suffix = isHighest ? '' : ' or higher';
    clipCountEl.textContent = `${totalCount} clip${totalCount !== 1 ? 's' : ''} at ${levelName}${suffix}`;
}

function showLoading() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');
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

function renderClips() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    noClipsEl.classList.add('hidden');

    clipsGridEl.innerHTML = clips.map(clip =>
        renderCard(clip, { playing: playingClipId === clip.clip_id })
    ).join('');
}

function appendClipsToGrid(newClips) {
    const newCardsHtml = newClips.map(clip => renderCard(clip)).join('');

    const loader = document.getElementById('bottom-loader');
    if (loader) {
        loader.insertAdjacentHTML('beforebegin', newCardsHtml);
    } else {
        clipsGridEl.insertAdjacentHTML('beforeend', newCardsHtml);
    }
}

function openClip(clipId) {
    const clip = clips.find(c => c.clip_id === clipId);
    if (!clip || !clip.embed_url) return;
    if (playingClipId === clip.clip_id) return;

    stopCurrentClip();
    playingClipId = clip.clip_id;

    const clipCard = document.querySelector(`[data-clip-id="${clip.clip_id}"]`);
    if (!clipCard) return;

    const thumbnailContainer = clipCard.querySelector('.clip-thumbnail');
    if (!thumbnailContainer) return;

    thumbnailContainer.innerHTML = renderPlayer(clip);
    clipCard.classList.add('playing');
}

function stopCurrentClip() {
    if (!playingClipId) return;

    const playingCard = document.querySelector(`[data-clip-id="${playingClipId}"]`);
    if (playingCard) {
        const clip = clips.find(c => c.clip_id === playingClipId);
        if (clip) {
            const thumbnailContainer = playingCard.querySelector('.clip-thumbnail');
            if (thumbnailContainer) {
                thumbnailContainer.innerHTML = renderThumbnail(clip);
            }
        }
        playingCard.classList.remove('playing');
    }

    playingClipId = null;
}

function initializeInfiniteScroll() {
    window.addEventListener('scroll', handleScroll);
}

function handleScroll() {
    if (scrollTimeout) {
        clearTimeout(scrollTimeout);
    }

    scrollTimeout = setTimeout(() => {
        const scrollPosition = window.innerHeight + window.scrollY;
        const documentHeight = document.documentElement.scrollHeight;

        if (documentHeight - scrollPosition < SCROLL_THRESHOLD) {
            loadMoreClips();
        }
    }, 100);
}

function loadMoreClips() {
    if (isLoading || !hasMore || clips.length === 0) return;

    const prefetched = ClipFeed.takePrefetched(selectedThreshold);
    if (prefetched) {
        applyPage(prefetched, false);
        return;
    }

    loadClips(false);
}

function showBottomLoader() {
    hideBottomLoader();

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

// Referenced directly from inline onclick/onchange attributes in index.html.
window.onFilterChange = onFilterChange;
window.loadClips = loadClips;
window.openClip = openClip;
