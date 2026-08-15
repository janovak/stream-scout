// StreamScout — render module: one clip-card template, one thumbnail
// fallback, one player embed, one escape rule.
//
// `getIntensityLabel` reads the `INTENSITY_LEVELS` ladder declared in
// app.js. It stays a global (no bundler, three plain <script> tags) —
// safe because rendering only ever runs after all three scripts have
// loaded, well before any card is actually drawn.

const THUMBNAIL_FALLBACK =
    'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 9%22>' +
    '<rect fill=%22%231f1f23%22 width=%2216%22 height=%229%22/></svg>';

function escapeAttr(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function getIntensityLabel(intensity) {
    if (intensity === null || intensity === undefined) {
        return { label: 'Unknown', threshold: 0 };
    }
    for (let i = INTENSITY_LEVELS.length - 1; i >= 0; i--) {
        if (intensity >= INTENSITY_LEVELS[i].threshold) {
            return INTENSITY_LEVELS[i];
        }
    }
    return { label: 'Unknown', threshold: 0 };
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

function renderThumbnail(clip) {
    return `
        <img src="${escapeAttr(clip.thumbnail_url)}"
             alt="Clip from ${escapeAttr(clip.streamer_login || 'Unknown')}"
             onerror="this.src='${THUMBNAIL_FALLBACK}'">
        <div class="play-overlay">
            <div class="play-icon"></div>
        </div>
    `;
}

function renderPlayer(clip) {
    // No autoplay — matches the previous openClip behaviour, not renderClips'.
    const embedUrl = clip.embed_url + '&parent=' + window.location.hostname;
    return `
        <div class="inline-player">
            <iframe src="${escapeAttr(embedUrl)}"
                    allowfullscreen
                    allow="autoplay; encrypted-media"></iframe>
        </div>
    `;
}

function renderCard(clip, { playing = false } = {}) {
    const intensityInfo = getIntensityLabel(clip.intensity);

    return `
        <div class="clip-card${playing ? ' playing' : ''}" data-clip-id="${escapeAttr(clip.clip_id)}" onclick="openClip(this.dataset.clipId)">
            <div class="clip-thumbnail">
                ${playing ? renderPlayer(clip) : renderThumbnail(clip)}
            </div>
            <div class="clip-meta">
                <h3>${escapeAttr(clip.streamer_login || 'Unknown Streamer')}</h3>
                <div class="clip-details">
                    <span class="intensity-badge" data-level="${intensityInfo.threshold}">${intensityInfo.label}</span>
                    <span class="clip-time">${formatTime(clip.detected_at)}</span>
                </div>
            </div>
        </div>
    `;
}
