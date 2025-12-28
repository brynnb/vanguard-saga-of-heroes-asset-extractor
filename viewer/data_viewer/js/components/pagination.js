import { state } from '../state.js';

export function updatePagination(total, page, pageSize, containerId, infoId, prevId, nextId, infoPrefix = "Page") {
    const pagination = document.getElementById(containerId);
    const totalPages = Math.ceil(total / pageSize);

    if (totalPages <= 1) {
        pagination.style.display = "none";
        return;
    }

    pagination.style.display = "flex";
    document.getElementById(infoId).textContent = `${infoPrefix} ${page + 1} of ${totalPages} (${total.toLocaleString()} total)`;
    document.getElementById(prevId).disabled = page === 0;
    document.getElementById(nextId).disabled = page >= totalPages - 1;
}

// Specific wrappers for easier use
export function updateExportsPagination() {
    updatePagination(state.totalExports, state.currentPage, state.pageSize, "pagination", "pageInfo", "prevBtn", "nextBtn");
}

export function updateFilesPagination() {
    updatePagination(state.totalFiles, state.currentFilePage, state.filePageSize, "filesPagination", "filesPageInfo", "prevFilesBtn", "nextFilesBtn", "Page");
}

export function updateTablePagination() {
    updatePagination(state.totalTableRows, state.currentTablePage, state.tablePageSize, "tablePagination", "tablePageInfo", "prevTableBtn", "nextTableBtn");
}
