-- Migration: Universal binary parsing tables
-- Target: /Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db
-- 
-- Design Philosophy:
-- Instead of one table per class type (staticmesh_data, texture_data, etc.),
-- we use a flexible EAV (Entity-Attribute-Value) approach combined with
-- structured JSON for complex nested data. This scales to any number of
-- binary types without schema changes.

-- Drop if they exist (for re-running)
DROP TABLE IF EXISTS parse_sessions;
DROP TABLE IF EXISTS parsed_exports;
DROP TABLE IF EXISTS unknown_regions;
DROP TABLE IF EXISTS parsed_fields;
DROP TABLE IF EXISTS field_observations;
DROP VIEW IF EXISTS non_compliant_exports;
DROP VIEW IF EXISTS unknown_patterns;
DROP VIEW IF EXISTS class_coverage;

-- Track parsing sessions for reproducibility
CREATE TABLE parse_sessions (
    id INTEGER PRIMARY KEY,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    parser_version TEXT NOT NULL,
    target_class TEXT,  -- e.g., 'StaticMesh', 'Texture', 'all'
    files_processed INTEGER DEFAULT 0,
    exports_processed INTEGER DEFAULT 0,
    total_bytes_parsed INTEGER DEFAULT 0,
    total_bytes_unknown INTEGER DEFAULT 0,
    notes TEXT
);

-- Detailed parsing status per export
-- Links to the 'files' table via file_path
CREATE TABLE parsed_exports (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id),
    export_index INTEGER NOT NULL,
    export_name TEXT NOT NULL,
    class_name TEXT NOT NULL,
    serial_offset INTEGER NOT NULL,
    serial_size INTEGER NOT NULL,
    -- Parsing metrics
    bytes_parsed INTEGER DEFAULT 0,
    bytes_unknown INTEGER DEFAULT 0,
    coverage_pct REAL GENERATED ALWAYS AS (
        CASE WHEN serial_size > 0 THEN ROUND(bytes_parsed * 100.0 / serial_size, 2) ELSE 0 END
    ) STORED,
    -- Compliance flags
    parse_status TEXT DEFAULT 'pending' CHECK(parse_status IN ('pending', 'partial', 'complete', 'error')),
    uses_heuristics INTEGER DEFAULT 0,  -- 1 = NOT COMPLIANT
    uses_skips INTEGER DEFAULT 0,       -- 1 = NOT COMPLIANT
    -- Timestamps
    session_id INTEGER REFERENCES parse_sessions(id),
    last_parsed_at TEXT,
    error_message TEXT,
    -- Raw data for re-analysis (can be large, optional)
    raw_hex TEXT,
    UNIQUE(file_id, export_index)
);

-- Track specific unknown byte regions for investigation
CREATE TABLE unknown_regions (
    id INTEGER PRIMARY KEY,
    parsed_export_id INTEGER NOT NULL REFERENCES parsed_exports(id),
    offset_start INTEGER NOT NULL,
    offset_end INTEGER NOT NULL,
    size INTEGER GENERATED ALWAYS AS (offset_end - offset_start) STORED,
    raw_hex TEXT NOT NULL,
    context TEXT,           -- Path in structure, e.g., "LODModel[0].post_vertices"
    hypothesis TEXT,
    observed_in_files TEXT, -- JSON array of file names
    sample_count INTEGER DEFAULT 1,
    resolved INTEGER DEFAULT 0,
    resolved_as TEXT,
    resolved_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Universal field storage using EAV pattern
-- Every field from every parsed export goes here
-- This replaces per-class tables like staticmesh_data
CREATE TABLE parsed_fields (
    id INTEGER PRIMARY KEY,
    parsed_export_id INTEGER NOT NULL REFERENCES parsed_exports(id),
    -- Field location
    field_path TEXT NOT NULL,    -- Hierarchical path: "bounds.bbox.min.x" or "lods[0].vertices[5].position.x"
    offset_start INTEGER,        -- Byte offset in export where this field starts
    offset_end INTEGER,          -- Byte offset where it ends
    -- Field metadata
    field_type TEXT NOT NULL,    -- int32, uint16, float, FVector, TArray, struct, unknown
    array_index INTEGER,         -- If part of an array, which index
    is_unknown INTEGER DEFAULT 0,-- 1 if we don't know what this field means
    -- Values (store all representations for flexibility)
    value_int INTEGER,
    value_float REAL,
    value_text TEXT,             -- For strings, enums, or display
    value_hex TEXT,              -- Raw hex for unknowns or complex types
    value_json TEXT,             -- For nested structures (arrays of structs, etc.)
    -- Index for fast queries
    UNIQUE(parsed_export_id, field_path, array_index)
);

-- Track field values across many files to find patterns
CREATE TABLE field_observations (
    id INTEGER PRIMARY KEY,
    field_path TEXT NOT NULL,    -- e.g., "StaticMesh.lod_version_flag"
    class_name TEXT NOT NULL,
    value_hex TEXT NOT NULL,
    value_int INTEGER,
    value_float REAL,
    occurrence_count INTEGER DEFAULT 1,
    sample_files TEXT,           -- JSON array of file names where this was seen
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(field_path, class_name, value_hex)
);

-- Indexes
CREATE INDEX idx_parsed_exports_file ON parsed_exports(file_id);
CREATE INDEX idx_parsed_exports_class ON parsed_exports(class_name);
CREATE INDEX idx_parsed_exports_status ON parsed_exports(parse_status);
CREATE INDEX idx_parsed_exports_coverage ON parsed_exports(coverage_pct);
CREATE INDEX idx_unknown_regions_export ON unknown_regions(parsed_export_id);
CREATE INDEX idx_unknown_regions_context ON unknown_regions(context);
CREATE INDEX idx_unknown_regions_resolved ON unknown_regions(resolved);
CREATE INDEX idx_parsed_fields_export ON parsed_fields(parsed_export_id);
CREATE INDEX idx_parsed_fields_path ON parsed_fields(field_path);
CREATE INDEX idx_parsed_fields_unknown ON parsed_fields(is_unknown);
CREATE INDEX idx_field_observations_path ON field_observations(field_path);
CREATE INDEX idx_field_observations_class ON field_observations(class_name);

-- View: All non-compliant exports
CREATE VIEW non_compliant_exports AS
SELECT 
    pe.id,
    f.file_name,
    pe.export_name,
    pe.class_name,
    pe.serial_size as bytes_total,
    pe.bytes_parsed,
    pe.bytes_unknown,
    pe.coverage_pct,
    pe.uses_heuristics,
    pe.uses_skips,
    pe.parse_status,
    pe.error_message
FROM parsed_exports pe
JOIN files f ON pe.file_id = f.id
WHERE pe.coverage_pct < 100.0 
   OR pe.uses_heuristics = 1 
   OR pe.uses_skips = 1
   OR pe.parse_status != 'complete';

-- View: Coverage by class
CREATE VIEW class_coverage AS
SELECT 
    class_name,
    COUNT(*) as export_count,
    SUM(CASE WHEN coverage_pct = 100 AND uses_heuristics = 0 AND uses_skips = 0 THEN 1 ELSE 0 END) as fully_compliant,
    ROUND(AVG(coverage_pct), 2) as avg_coverage,
    SUM(bytes_parsed) as total_bytes_parsed,
    SUM(bytes_unknown) as total_bytes_unknown
FROM parsed_exports
GROUP BY class_name
ORDER BY export_count DESC;

-- View: Unknown region patterns (to find commonalities)
CREATE VIEW unknown_patterns AS
SELECT 
    context,
    size,
    COUNT(*) as occurrence_count,
    GROUP_CONCAT(DISTINCT raw_hex) as distinct_values,
    MAX(hypothesis) as hypothesis,
    SUM(resolved) as resolved_count
FROM unknown_regions
GROUP BY context, size
HAVING COUNT(*) > 1
ORDER BY occurrence_count DESC;
