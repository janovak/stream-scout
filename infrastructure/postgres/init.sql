-- Twitch Stream Highlights System - Database Schema

-- Streamers table: stores metadata about monitored streamers
CREATE TABLE streamers (
    streamer_id BIGINT PRIMARY KEY,
    streamer_login VARCHAR(255) NOT NULL,
    allows_clipping BOOLEAN DEFAULT TRUE,
    first_seen_at TIMESTAMP DEFAULT NOW(),
    last_seen_at TIMESTAMP DEFAULT NOW()
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
    detected_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_clips_detected_at ON clips(detected_at DESC);
CREATE INDEX idx_clips_broadcaster_id ON clips(broadcaster_id);
CREATE INDEX idx_clips_created_at ON clips(created_at DESC);

-- Add foreign key relationship (optional, as streamers may be transient)
-- ALTER TABLE clips ADD CONSTRAINT fk_clips_broadcaster
--     FOREIGN KEY (broadcaster_id) REFERENCES streamers(streamer_id);
