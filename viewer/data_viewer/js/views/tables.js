import { state, API_BASE } from '../state.js';
import { updateTablePagination } from '../components/pagination.js';

export async function loadTableCounts() {
    try {
        const response = await fetch(`${API_BASE}/table_counts`);
        const counts = await response.json();

        // Update counts in sidebar
        for (const [table, count] of Object.entries(counts)) {
            const el = document.getElementById(`count-${table}`);
            if (el) el.textContent = count.toLocaleString();
        }
    } catch (e) {
        console.error("Failed to load table counts:", e);
    }
}

export function showTableView(tableName) {
    document.getElementById('fileExplorer').style.display = 'none';
    document.getElementById('classExplorer').style.display = 'none';
    document.getElementById('tableExplorer').style.display = 'flex';

    state.currentTable = tableName;
    state.currentTablePage = 0;

    // Update sidebar active state
    document.querySelectorAll('.class-item').forEach(el => el.classList.remove('active'));

    // Try both underscored and hyphenated versions of the table name for the ID
    let navId = `nav-${tableName}`;
    let navItem = document.getElementById(navId) || document.getElementById(navId.replace('_', '-'));
    if (!navItem && tableName === 'exports') navItem = document.getElementById('nav-exports-table');

    if (navItem) navItem.classList.add('active');

    const titleEl = document.getElementById('tableTitle');
    if (titleEl) titleEl.textContent = `Table: ${tableName}`;
    loadTableData();
}

export async function loadTableData() {
    if (!state.currentTable) return;

    const search = document.getElementById('tableSearch').value;
    const params = new URLSearchParams({
        table: state.currentTable,
        limit: state.tablePageSize,
        offset: state.currentTablePage * state.tablePageSize
    });

    if (search) params.append('search', search);

    try {
        const response = await fetch(`${API_BASE}/table_data?${params}`);
        const data = await response.json();

        state.totalTableRows = data.total;
        renderTableData(data);
        updateTablePagination();

    } catch (e) {
        document.getElementById('tableData').innerHTML = '<p class="error">Failed to load table data: ' + e.message + '</p>';
    }
}

export function renderTableData(data) {
    const container = document.getElementById('tableData');
    if (!data.rows || !data.rows.length) {
        container.innerHTML = '<p class="empty">No data found in this table</p>';
        return;
    }

    const columns = data.columns;

    container.innerHTML = `
        <table class="data-table">
            <thead>
                <tr>
                    ${columns.map(col => `<th>${col}</th>`).join('')}
                </tr>
            </thead>
            <tbody>
                ${data.rows.map(row => `
                    <tr>
                        ${columns.map(col => {
        let val = row[col];
        if (val === null || val === undefined) val = '<span class="null">null</span>';
        else if (typeof val === 'number') val = val.toLocaleString();
        else if (typeof val === 'string' && val.length > 100) val = `<span title="${val}">${val.substring(0, 100)}...</span>`;
        return `<td>${val}</td>`;
    }).join('')}
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

export function clearTableSearch() {
    document.getElementById('tableSearch').value = '';
    state.currentTablePage = 0;
    loadTableData();
}

export async function runQuery() {
    const sql = document.getElementById("sqlInput").value;
    const container = document.getElementById("sqlResults");

    container.innerHTML = '<p class="loading">Running query...</p>';

    try {
        const response = await fetch(
            `${API_BASE}/query?sql=${encodeURIComponent(sql)}`
        );
        const data = await response.json();

        if (data.error) {
            container.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }

        if (!data.rows.length) {
            container.innerHTML = '<p class="empty">No results</p>';
            return;
        }

        container.innerHTML = `
              <p style="color: #888; margin-bottom: 0.5rem;">${data.rows.length} rows returned</p>
              <div style="overflow-x: auto;">
                  <table>
                      <thead>
                          <tr>
                              ${data.columns
                .map((c) => `<th>${c}</th>`)
                .join("")}
                          </tr>
                      </thead>
                      <tbody>
                          ${data.rows
                .map(
                    (row) => `
                              <tr>
                                  ${data.columns
                            .map(
                                (c, i) => `<td>${row[i] ?? "-"}</td>`
                            )
                            .join("")}
                              </tr>
                          `
                )
                .join("")}
                      </tbody>
                  </table>
              </div>
          `;
    } catch (e) {
        container.innerHTML = `<p class="error">Query failed: ${e.message}</p>`;
    }
}

export function prevTablePage() {
    if (state.currentTablePage > 0) {
        state.currentTablePage--;
        loadTableData();
    }
}

export function nextTablePage() {
    const totalPages = Math.ceil(state.totalTableRows / state.tablePageSize);
    if (state.currentTablePage < totalPages - 1) {
        state.currentTablePage++;
        loadTableData();
    }
}

export function setQuery(sql) {
    const input = document.getElementById("sqlInput");
    if (input) input.value = sql;
}
