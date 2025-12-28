import { API_BASE } from '../state.js';

export async function showExportInPanel(exportId, rowElement) {
    const content = document.getElementById('detailPanelContent');

    // Highlight selected row
    document.querySelectorAll('#exportsTable tr.selected, #filesList tr.selected, #tableData tr.selected').forEach(tr => tr.classList.remove('selected'));
    if (rowElement && rowElement.tagName === 'TR') rowElement.classList.add('selected');

    // Show loading
    content.innerHTML = '<p class="empty">Loading...</p>';

    try {
        const response = await fetch(`${API_BASE}/export_detail?id=${exportId}`);
        const data = await response.json();

        if (data.error) {
            content.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }

        const exp = data.export;

        let html = `
            <div class="detail-row">
                <span class="detail-label">Name</span>
                <span class="detail-value">${exp.object_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Class</span>
                <span class="detail-value">${exp.class_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Chunk</span>
                <span class="detail-value">${exp.chunk_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Size</span>
                <span class="detail-value">${exp.serial_size} bytes</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Offset</span>
                <span class="detail-value">0x${exp.serial_offset?.toString(16) || 'N/A'}</span>
            </div>
        `;

        if (exp.position_x !== null && exp.position_x !== undefined) {
            html += `
                <div class="detail-row">
                    <span class="detail-label">Position</span>
                    <span class="detail-value">${exp.position_x?.toFixed(1)}, ${exp.position_y?.toFixed(1)}, ${exp.position_z?.toFixed(1)}</span>
                </div>
            `;
        }

        if (exp.mesh_ref) {
            html += `
                <div class="detail-row">
                    <span class="detail-label">Mesh Ref</span>
                    <span class="detail-value">${exp.mesh_ref}</span>
                </div>
            `;
        }

        if (exp.prefab_name) {
            html += `
                <div class="detail-row">
                    <span class="detail-label">Prefab</span>
                    <span class="detail-value">${exp.prefab_name}</span>
                </div>
            `;
        }

        // Properties section
        if (data.properties && data.properties.length > 0) {
            html += `
                <h4 style="color: #e94560; margin: 1rem 0 0.5rem; font-size: 0.9rem;">Properties (${data.properties.length})</h4>
            `;
            for (const prop of data.properties) {
                html += `
                    <div class="detail-row">
                        <span class="detail-label">${prop.prop_name}${prop.struct_name ? ` (${prop.struct_name})` : ""}</span>
                        <span class="detail-value">${prop.value_text || '-'}</span>
                    </div>
                `;
            }
        } else {
            html += `<p style="color: #666; margin-top: 1rem; font-size: 0.85rem;">No properties parsed</p>`;
        }

        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<p class="error">Error: ${e.message}</p>`;
    }
}

export function closeDetailPanel() {
    const content = document.getElementById('detailPanelContent');
    content.innerHTML = '<p class="empty">Click an export to view details</p>';
    document.querySelectorAll('tr.selected').forEach(tr => tr.classList.remove('selected'));
}
