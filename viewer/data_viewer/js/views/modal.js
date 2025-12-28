import { API_BASE } from '../state.js';

export async function showFileStructure(filePath) {
    const modal = document.getElementById('structureModal');
    const title = document.getElementById('structureModalTitle');
    const stats = document.getElementById('structureStats');
    const content = document.getElementById('structureContent');

    // Show modal with loading state
    modal.classList.add('active');
    title.textContent = filePath.split('/').pop();
    stats.innerHTML = '';
    content.innerHTML = '<p style="color:#888">Loading structure...</p>';

    try {
        const response = await fetch(`${API_BASE}/file_structure?path=${encodeURIComponent(filePath)}`);
        const data = await response.json();

        if (data.error) {
            content.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }

        // Show stats
        const coverage = data.parsed_bytes && data.total_bytes
            ? ((data.parsed_bytes / data.total_bytes) * 100).toFixed(1)
            : '?';
        stats.innerHTML = `
      <div>Type: <span class="stat-value">${data.type || 'Unknown'}</span></div>
      <div>Export: <span class="stat-value">${data.export_name || '-'}</span></div>
      <div>Size: <span class="stat-value">${data.total_bytes} bytes</span></div>
      <div>Parsed: <span class="stat-value">${data.parsed_bytes} bytes (${coverage}%)</span></div>
    `;

        // Render structure
        content.innerHTML = renderStructure(data.sections || []);

    } catch (e) {
        content.innerHTML = `<p class="error">Failed to load structure: ${e.message}</p>`;
    }
}

export function closeStructureModal(event) {
    // If called from overlay click, only close if target is overlay
    if (event && event.target !== document.getElementById('structureModal')) {
        return;
    }
    document.getElementById('structureModal').classList.remove('active');
}

export function renderStructure(sections) {
    if (!sections || sections.length === 0) {
        return '<p style="color:#666">No structure data available</p>';
    }

    return sections.map(section => {
        const hasWarning = section.fields && section.fields.some(f => f.warning);
        const warningClass = hasWarning ? 'style="border-color: #ff6b6b;"' : '';

        const fieldsHtml = (section.fields || []).map(field => {
            // Format value
            let valueStr = String(field.value);
            if (typeof field.value === 'number' && !Number.isInteger(field.value)) {
                valueStr = field.value.toFixed(6);
            }

            const warningHtml = field.warning
                ? `<span class="field-warning">⚠ ${field.warning}</span>`
                : '';

            return `
        <div class="field-row">
          <span class="field-name">${field.name}</span>
          <span class="field-value">${valueStr}</span>
          ${warningHtml}
        </div>
      `;
        }).join('');

        return `
      <div class="structure-section" ${warningClass}>
        <div class="section-header">
          <span class="section-name">${section.name}</span>
          <span class="section-meta">Offset: ${section.offset} | Size: ${section.size} bytes</span>
        </div>
        <div class="section-fields">
          ${fieldsHtml || '<em style="color:#666">No fields</em>'}
        </div>
      </div>
    `;
    }).join('');
}
