-- Migration 003: Add parsing status tracking improvements
-- Date: 2024-12-24

-- Add gltf_exported column to parsed_exports
ALTER TABLE parsed_exports ADD COLUMN gltf_exported INTEGER DEFAULT 0;

-- Add parsing summary view
CREATE VIEW IF NOT EXISTS parsing_summary AS
SELECT 
    class_name,
    COUNT(*) as total_exports,
    SUM(CASE WHEN parse_status = 'complete' THEN 1 ELSE 0 END) as complete_count,
    SUM(CASE WHEN parse_status = 'error' THEN 1 ELSE 0 END) as error_count,
    SUM(CASE WHEN parse_status = 'pending' THEN 1 ELSE 0 END) as pending_count,
    ROUND(AVG(CASE WHEN coverage_pct > 0 THEN coverage_pct ELSE NULL END), 2) as avg_coverage,
    SUM(bytes_parsed) as total_bytes_parsed,
    SUM(bytes_unknown) as total_bytes_unknown,
    SUM(gltf_exported) as gltf_exported_count
FROM parsed_exports
GROUP BY class_name;

-- Add file parsing status view  
CREATE VIEW IF NOT EXISTS file_parsing_status AS
SELECT 
    f.id as file_id,
    f.file_name,
    f.file_path,
    COUNT(pe.id) as total_exports,
    SUM(CASE WHEN pe.parse_status = 'complete' THEN 1 ELSE 0 END) as complete_exports,
    SUM(CASE WHEN pe.parse_status = 'error' THEN 1 ELSE 0 END) as error_exports,
    ROUND(AVG(pe.coverage_pct), 2) as avg_coverage,
    MAX(pe.last_parsed_at) as last_parsed
FROM files f
LEFT JOIN parsed_exports pe ON pe.file_id = f.id
WHERE f.extension = '.usx'
GROUP BY f.id;

-- Add class coverage view (for data viewer)
CREATE VIEW IF NOT EXISTS class_coverage AS
SELECT 
    class_name,
    COUNT(*) as total,
    SUM(CASE WHEN coverage_pct >= 100 THEN 1 ELSE 0 END) as fully_parsed,
    SUM(CASE WHEN coverage_pct > 0 AND coverage_pct < 100 THEN 1 ELSE 0 END) as partial,
    SUM(CASE WHEN coverage_pct = 0 OR coverage_pct IS NULL THEN 1 ELSE 0 END) as unparsed,
    ROUND(100.0 * SUM(CASE WHEN coverage_pct >= 100 THEN 1 ELSE 0 END) / COUNT(*), 1) as pct_complete
FROM parsed_exports
GROUP BY class_name
ORDER BY total DESC;
