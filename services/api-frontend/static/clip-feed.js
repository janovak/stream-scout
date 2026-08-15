// StreamScout — data module: owns the clip-list request, paging, the
// prefetch cache and the response shape. `page()` throws on failure;
// `prefetch()` swallows so a failed background fetch never surfaces to the
// user (it just falls back to a live fetch on demand).

const ClipFeed = (() => {
    const API_BASE = '/v1.0';
    const PAGE_SIZE = 24;

    let prefetchedThreshold = null;
    let prefetchedPage = null;

    function buildUrl(threshold, offset) {
        return `${API_BASE}/clip?min_intensity=${threshold}&limit=${PAGE_SIZE}&offset=${offset}`;
    }

    function toPage(data) {
        return {
            clips: data.clips || [],
            totalCount: data.total_count || 0,
            hasMore: data.has_more || false
        };
    }

    function preloadThumbnails(clips) {
        clips.forEach(clip => {
            if (clip.thumbnail_url) {
                const img = new Image();
                img.src = clip.thumbnail_url;
            }
        });
    }

    async function page(threshold, offset) {
        const response = await fetch(buildUrl(threshold, offset));
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return toPage(await response.json());
    }

    function prefetch(threshold, offset) {
        prefetchedThreshold = null;
        prefetchedPage = null;

        page(threshold, offset)
            .then(result => {
                prefetchedThreshold = threshold;
                prefetchedPage = result;
                preloadThumbnails(result.clips);
            })
            .catch(err => {
                console.warn('Prefetch failed (will fetch on demand):', err);
                prefetchedThreshold = null;
                prefetchedPage = null;
            });
    }

    function takePrefetched(threshold) {
        if (prefetchedPage === null || prefetchedThreshold !== threshold) {
            return null;
        }
        const result = prefetchedPage;
        prefetchedThreshold = null;
        prefetchedPage = null;
        return result;
    }

    function reset() {
        prefetchedThreshold = null;
        prefetchedPage = null;
    }

    return { PAGE_SIZE, page, prefetch, takePrefetched, reset };
})();
