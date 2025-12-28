import { state, API_BASE } from '../state.js';
import { formatBytes, getSortIndicator } from '../utils.js';
import { updateFilesPagination } from '../components/pagination.js';
import { showFileStructure } from './modal.js';

export async function loadFiles() {
    const search = document.getElementById('fileSearch').value;
    const category = document.getElementById('fileCategory').value;
    const ext = document.getElementById('fileExt').value;

    const params = new URLSearchParams({
        limit: state.filePageSize,
        offset: state.currentFilePage * state.filePageSize,
        sort: state.fileSortField,
        order: state.fileSortOrder
    });

    if (search) params.append('search', search);
    if (category) params.append('category', category);
    if (ext) params.append('extension', ext);

    try {
        const response = await fetch(`${API_BASE}/files?${params}`);
        const data = await response.json();

        state.totalFiles = data.total;
        renderFilesTable(data.rows);
        updateFilesPagination();

    } catch (e) {
        document.getElementById('filesList').innerHTML = '<p class="error">Failed to load files: ' + e.message + '</p>';
    }
}

export function renderFilesTable(rows) {
    const container = document.getElementById('filesList');
    if (!rows.length) {
        container.innerHTML = '<p class="empty">No files found</p>';
        return;
    }

    container.innerHTML = `
      <table>
          <thead>
              <tr>
                  <th style="width: 25%; cursor: pointer;" onclick="window.sortFiles('location')">Location ${getSortIndicator('location', state.fileSortField, state.fileSortOrder)}</th>
                  <th style="cursor: pointer;" onclick="window.sortFiles('file_name')">Name ${getSortIndicator('file_name', state.fileSortField, state.fileSortOrder)}</th>
                  <th style="width: 80px; cursor: pointer;" onclick="window.sortFiles('extension')">Ext ${getSortIndicator('extension', state.fileSortField, state.fileSortOrder)}</th>
                  <th style="width: 100px; cursor: pointer;" onclick="window.sortFiles('size_bytes')">Size ${getSortIndicator('size_bytes', state.fileSortField, state.fileSortOrder)}</th>
                  <th style="width: 80px;">Cov %</th>
                  <th style="width: 60px;">Extr.</th>
                  <th>Notes</th>
              </tr>
          </thead>
          <tbody>
              ${rows.map(f => {
        // Coverage Color
        let covColor = '#ff6b6b';
        if (f.parse_coverage_pct > 99) covColor = '#4ecdc4';
        else if (f.parse_coverage_pct > 50) covColor = '#ffe66d';

        // Extracted Icon
        const extrIcon = f.is_extracted ? '<span style="color:#4ecdc4">✓</span>' : '<span style="color:#666">✗</span>';

        return `
                  <tr>
                      <td style="color:#888; font-size:0.8em; word-break: break-all;" title="${f.location}">${f.location || './'}</td>
                      <td>
                          <button onclick="window.showFileStructure('${f.file_path}')" style="background:none; border:none; color:#4ecdc4; font-weight:500; cursor:pointer; text-align:left; font-size:inherit;">
                              ${f.file_name}
                          </button>
                      </td>
                      <td><span style="background:#222; padding:2px 4px; border-radius:3px; font-size:0.8em; color:#aaa;">${f.extension}</span></td>
                      <td class="number" style="font-size:0.9em;">${formatBytes(f.size_bytes)}</td>
                      <td class="number">
                          <span style="color:${covColor}">${f.parse_coverage_pct ? f.parse_coverage_pct.toFixed(1) + '%' : '-'}</span>
                      </td>
                      <td style="text-align:center;">${extrIcon}</td>
                      <td style="font-size:0.8em; color:#888; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:200px;" title="${f.parser_notes || ''}">
                          ${f.parser_notes || '-'}
                      </td>
                  </tr>
                  `;
    }).join('')}
          </tbody>
      </table>
    `;
}

export function sortFiles(field) {
    if (state.fileSortField === field) {
        state.fileSortOrder = state.fileSortOrder === 'asc' ? 'desc' : 'asc';
    } else {
        state.fileSortField = field;
        state.fileSortOrder = (field === 'size_bytes' || field === 'modified_time') ? 'desc' : 'asc';
    }
    state.currentFilePage = 0;
    loadFiles();
}

export function clearFileFilters() {
    document.getElementById('fileSearch').value = '';
    document.getElementById('fileCategory').value = '';
    document.getElementById('fileExt').value = '';
    state.currentFilePage = 0;
    loadFiles();
}

export function showFilesView(categoryPreset = null, extPreset = null) {
    document.getElementById('fileExplorer').style.display = 'flex';
    document.getElementById('classExplorer').style.display = 'none';
    const tableExplorer = document.getElementById('tableExplorer');
    if (tableExplorer) tableExplorer.style.display = 'none';

    // Reset filters if preset provided
    if (categoryPreset || extPreset) {
        document.getElementById('fileCategory').value = categoryPreset || '';
        document.getElementById('fileExt').value = extPreset || '';
        document.getElementById('fileSearch').value = '';
        state.currentFilePage = 0;
    }

    // Update Sidebar
    document.querySelectorAll('.class-item').forEach(el => el.classList.remove('active'));

    // Highlight correct nav item based on category
    if (categoryPreset === 'Mesh') document.getElementById('nav-meshes').classList.add('active');
    else if (categoryPreset === 'Texture' && extPreset === 'utx') document.getElementById('nav-textures').classList.add('active');
    else if (categoryPreset === 'Map' && extPreset === 'vgr') document.getElementById('nav-chunks').classList.add('active');
    else if (!categoryPreset && !extPreset) document.getElementById('nav-files').classList.add('active');

    loadFiles();
}

export function prevFilesPage() {
    if (state.currentFilePage > 0) {
        state.currentFilePage--;
        loadFiles();
    }
}

export function nextFilesPage() {
    const totalPages = Math.ceil(state.totalFiles / state.filePageSize);
    if (state.currentFilePage < totalPages - 1) {
        state.currentFilePage++;
        loadFiles();
    }
}
