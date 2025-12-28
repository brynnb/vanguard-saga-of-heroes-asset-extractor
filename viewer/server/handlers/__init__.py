"""
Handlers package - imports all handler functions.
"""

from .exports import (
    handle_class_summary,
    handle_chunks,
    handle_length_distribution,
    handle_exports,
    handle_names,
    handle_properties,
    handle_property_summary,
    handle_export_detail,
    handle_watervolume_chain,
)

from .files import (
    handle_files,
    handle_file_structure,
)

from .tables import (
    handle_query,
    handle_table_data,
    handle_table_counts,
)

from .parsing import (
    handle_parse_status,
    handle_class_coverage,
    handle_parsed_exports,
)
