import { state, API_BASE } from '../state.js';
import { updateExportsPagination } from '../components/pagination.js';
import { showExportInPanel } from '../components/exportPanel.js';

export async function loadClasses() {
    try {
        const response = await fetch(`${API_BASE}/class_summary`);
        const classes = await response.json();
        state.classes = classes;
        renderClassList();
    } catch (e) {
        document.getElementById("classList").innerHTML =
            '<li class="error">Failed to load classes</li>';
    }
}

export async function loadChunks() {
    try {
        const response = await fetch(`${API_BASE}/chunks`);
        const chunks = await response.json();
        state.chunks = chunks;

        const select = document.getElementById("chunkFilter");
        select.innerHTML = '<option value="">All Chunks</option>';
        chunks.forEach((chunk) => {
            const option = document.createElement("option");
            option.value = chunk.id;
            option.textContent = chunk.filename;
            select.appendChild(option);
        });
    } catch (e) {
        console.error("Failed to load chunks:", e);
    }
}

export function updateStats() {
    const totalExportsStats = (state.classes || []).reduce(
        (sum, c) => sum + c.export_count,
        0
    );
    document.getElementById("stats").textContent = `${state.chunks?.length || 0
        } chunks • ${totalExportsStats.toLocaleString()} exports • ${state.classes?.length || 0
        } classes`;
}

export function renderClassList() {
    const list = document.getElementById("classList");
    list.innerHTML = state.classes
        .map(
            (c) => `
          <li class="class-item ${state.currentClass === c.class_name ? "active" : ""
                }" 
              onclick="window.selectClass('${c.class_name}')">
              <span>${c.class_name}</span>
              <span class="count">${c.export_count.toLocaleString()}</span>
          </li>
      `
        )
        .join("");
}

export async function selectClass(className) {
    showClassView();
    state.currentClass = className;
    state.currentPage = 0;
    renderClassList();
    await loadLengthDistribution();
    await loadExports();
}

export function showClassView() {
    document.getElementById('fileExplorer').style.display = 'none';
    document.getElementById('classExplorer').style.display = 'flex';
    const tableExplorer = document.getElementById('tableExplorer');
    if (tableExplorer) tableExplorer.style.display = 'none';

    // Remove active class from all nav items
    document.querySelectorAll('.class-item').forEach(el => el.classList.remove('active'));
}

export async function loadLengthDistribution() {
    if (!state.currentClass) return;

    try {
        const response = await fetch(
            `${API_BASE}/length_distribution?class=${encodeURIComponent(
                state.currentClass
            )}`
        );
        state.lengthDistribution = await response.json();

        // Update length filter
        const select = document.getElementById("lengthFilter");
        select.innerHTML = '<option value="">All Lengths</option>';
        state.lengthDistribution.forEach((d) => {
            const option = document.createElement("option");
            option.value = d.data_length;
            option.textContent = `${d.data_length} bytes (${d.count})`;
            select.appendChild(option);
        });

        renderLengthChart();
    } catch (e) {
        console.error("Failed to load length distribution:", e);
    }
}

export function renderLengthChart() {
    const container = document.getElementById("lengthChart");

    if (!state.lengthDistribution.length) {
        container.innerHTML = '<p class="empty">No data</p>';
        return;
    }

    const maxCount = Math.max(...state.lengthDistribution.map((d) => d.count));

    container.innerHTML = state.lengthDistribution
        .slice(0, 20)
        .map(
            (d) => `
          <div class="bar-row">
              <div class="bar-label">${d.data_length} bytes</div>
              <div class="bar-container">
                  <div class="bar" style="width: ${(d.count / maxCount) * 100
                }%">
                      ${d.count} (${d.pct}%)
                  </div>
              </div>
          </div>
      `
        )
        .join("");

    if (state.lengthDistribution.length > 20) {
        container.innerHTML += `<p style="color: #888; margin-top: 0.5rem; font-size: 0.8rem;">
              ... and ${state.lengthDistribution.length - 20
            } more length variants
          </p>`;
    }
}

export async function loadExports() {
    if (!state.currentClass) return;

    const chunkId = document.getElementById("chunkFilter").value;
    const length = document.getElementById("lengthFilter").value;

    const params = new URLSearchParams({
        class: state.currentClass,
        limit: state.pageSize,
        offset: state.currentPage * state.pageSize,
    });

    if (chunkId) params.append("chunk_id", chunkId);
    if (length) params.append("length", length);

    try {
        const response = await fetch(`${API_BASE}/exports?${params}`);
        const data = await response.json();

        state.totalExports = data.total;
        renderExportsTable(data.rows);
        updateExportsPagination();
    } catch (e) {
        document.getElementById("exportsTable").innerHTML =
            '<p class="error">Failed to load exports</p>';
    }
}

export function renderExportsTable(rows) {
    const container = document.getElementById("exportsTable");

    if (!rows.length) {
        container.innerHTML = '<p class="empty">No exports found</p>';
        return;
    }

    container.innerHTML = `
          <table>
              <thead>
                  <tr>
                      <th>Object Name</th>
                      <th>Chunk</th>
                      <th>Length</th>
                      <th>Position (X, Y, Z)</th>
                  </tr>
              </thead>
              <tbody>
                  ${rows
            .map(
                (row) => `
                      <tr data-export-id="${row.id}" onclick="window.showExportInPanel(${row.id}, this)">
                          <td>${row.object_name}</td>
                          <td>${row.chunk_name}</td>
                          <td class="number">${row.data_length || row.serial_size || '-'}</td>
                          <td class="number">
                              ${row.position_x !== null && row.position_x !== undefined
                        ? `${row.position_x.toFixed(
                            0
                        )}, ${row.position_y.toFixed(
                            0
                        )}, ${row.position_z.toFixed(0)}`
                        : "-"
                    }
                          </td>
                      </tr>
                  `
            )
            .join("")}
              </tbody>
          </table>
      `;
}

export function clearFilters() {
    document.getElementById("chunkFilter").value = "";
    document.getElementById("lengthFilter").value = "";
    state.currentPage = 0;
    loadExports();
}

export function switchTab(tabName) {
    document
        .querySelectorAll(".tab")
        .forEach((t) => t.classList.remove("active"));

    const tabEl = document.querySelector(`.tab[data-tab="${tabName}"]`);
    if (tabEl) tabEl.classList.add("active");

    document.getElementById("exportsTab").style.display =
        tabName === "exports" ? "block" : "none";
    document.getElementById("propertiesTab").style.display =
        tabName === "properties" ? "block" : "none";
    document.getElementById("distributionTab").style.display =
        tabName === "distribution" ? "block" : "none";
    document.getElementById("sqlTab").style.display =
        tabName === "sql" ? "block" : "none";

    if (tabName === "properties" && state.currentClass) {
        loadPropertySummary();
    }
}

export async function loadPropertySummary() {
    if (!state.currentClass) return;

    document.getElementById("propsClassName").textContent = state.currentClass;
    const container = document.getElementById("propertiesList");
    container.innerHTML = '<p class="loading">Loading properties...</p>';

    try {
        const response = await fetch(
            `${API_BASE}/property_summary?class=${encodeURIComponent(
                state.currentClass
            )}`
        );
        const props = await response.json();

        if (!props.length) {
            container.innerHTML =
                '<p class="empty">No properties found for this class</p>';
            return;
        }

        container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Property Name</th>
            <th>Type</th>
            <th>Count</th>
            <th>Unique Values</th>
          </tr>
        </thead>
        <tbody>
          ${props
                .map(
                    (p) => `
            <tr style="cursor: pointer;" onclick="window.showPropertyValues('${p.prop_name}')">
              <td>${p.prop_name}</td>
              <td class="hex">${p.prop_type}</td>
              <td class="number">${p.count}</td>
              <td class="number">${p.unique_values || "-"}</td>
            </tr>
          `
                )
                .join("")}
        </tbody>
      </table>
    `;
    } catch (e) {
        container.innerHTML = `<p class="error">Failed to load properties: ${e.message}</p>`;
    }
}

export async function showPropertyValues(propName) {
    const container = document.getElementById("exportDetail");
    const propsContainer = document.getElementById("exportProperties");

    container.style.display = "block";
    document.getElementById(
        "detailExportName"
    ).textContent = `${state.currentClass}.${propName}`;
    propsContainer.innerHTML = '<p class="loading">Loading values...</p>';

    try {
        const response = await fetch(
            `${API_BASE}/properties?class=${encodeURIComponent(
                state.currentClass
            )}&prop_name=${encodeURIComponent(propName)}`
        );
        const values = await response.json();

        if (!values.length) {
            propsContainer.innerHTML = '<p class="empty">No values found</p>';
            return;
        }

        propsContainer.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Object</th>
            <th>Chunk</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          ${values
                .slice(0, 50)
                .map(
                    (v) => `
            <tr>
              <td>${v.object_name}</td>
              <td>${v.filename}</td>
              <td class="number">${v.value_text || "-"}</td>
            </tr>
          `
                )
                .join("")}
        </tbody>
      </table>
      ${values.length > 50
                ? `<p style="color: #888; margin-top: 0.5rem;">Showing 50 of ${values.length} values</p>`
                : ""
            }
    `;
    } catch (e) {
        propsContainer.innerHTML = `<p class="error">Failed to load values: ${e.message}</p>`;
    }
}

export function prevPage() {
    if (state.currentPage > 0) {
        state.currentPage--;
        loadExports();
    }
}

export function nextPage() {
    const totalPages = Math.ceil(state.totalExports / state.pageSize);
    if (state.currentPage < totalPages - 1) {
        state.currentPage++;
        loadExports();
    }
}
