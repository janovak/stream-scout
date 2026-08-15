-- Twitch Stream Highlights System - Database Schema

-- Streamers table: stores metadata about monitored streamers
CREATE TABLE streamers (
    streamer_id BIGINT PRIMARY KEY,
    streamer_login VARCHAR(255) NOT NULL,
    allows_clipping BOOLEAN DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_streamers_last_seen ON streamers(last_seen_at);
CREATE INDEX idx_streamers_login ON streamers(streamer_login);

-- Clips table: stores clip metadata created by anomaly detection
CREATE TABLE clips (
    id SERIAL PRIMARY KEY,
    broadcaster_id BIGINT NOT NULL,
    clip_id VARCHAR(255) NOT NULL UNIQUE,
    embed_url TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    intensity FLOAT,  -- Standard deviations above mean (Z-score), nullable for legacy clips
    duration FLOAT,   -- seconds, from Twitch Get Clips; null if video unavailable or offset not yet computed
    vod_offset INTEGER  -- seconds into the VOD where the clip starts; null under the same conditions as duration
);

CREATE INDEX idx_clips_detected_at ON clips(detected_at DESC);
CREATE INDEX idx_clips_broadcaster_id ON clips(broadcaster_id);
CREATE INDEX idx_clips_created_at ON clips(created_at DESC);
CREATE INDEX idx_clips_intensity_detected ON clips(intensity, detected_at DESC);  -- For filtered queries

-- Add foreign key relationship (optional, as streamers may be transient)
-- ALTER TABLE clips ADD CONSTRAINT fk_clips_broadcaster
--     FOREIGN KEY (broadcaster_id) REFERENCES streamers(streamer_id);
