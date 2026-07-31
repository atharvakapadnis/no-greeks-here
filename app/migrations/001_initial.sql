CREATE TABLE guest (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    device_token  TEXT UNIQUE,
    claimed_at    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE horse (
    number INTEGER PRIMARY KEY,
    name   TEXT
);

CREATE TABLE race (
    number       INTEGER PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('SCHEDULED', 'OPEN', 'LOCKED', 'SETTLED')),
    first        INTEGER REFERENCES horse(number),
    second       INTEGER REFERENCES horse(number),
    third        INTEGER REFERENCES horse(number),
    opened_at    TEXT,
    locked_at    TEXT,
    settled_at   TEXT,
    auto_lock_at TEXT,
    CHECK (
        status != 'SETTLED'
        OR (first IS NOT NULL AND second IS NOT NULL AND third IS NOT NULL)
    ),
    CHECK (
        first IS NULL OR second IS NULL OR third IS NULL
        OR (first <> second AND second <> third AND first <> third)
    )
);

-- At most one OPEN race at a time.
CREATE UNIQUE INDEX idx_race_one_open ON race(status) WHERE status = 'OPEN';

CREATE TABLE race_entry (
    race_number  INTEGER NOT NULL REFERENCES race(number),
    horse_number INTEGER NOT NULL REFERENCES horse(number),
    scratched    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (race_number, horse_number)
);

CREATE TABLE bet (
    id             INTEGER PRIMARY KEY,
    race_number    INTEGER NOT NULL REFERENCES race(number),
    guest_id       INTEGER NOT NULL REFERENCES guest(id),
    horse_number   INTEGER NOT NULL REFERENCES horse(number),
    client_bet_id  TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL,
    superseded_at  TEXT
);

-- At most one live (non-superseded) bet per guest per race.
CREATE UNIQUE INDEX idx_bet_one_live_per_guest_race
    ON bet(race_number, guest_id) WHERE superseded_at IS NULL;

CREATE INDEX idx_bet_race_number ON bet(race_number);

CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
