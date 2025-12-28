// Automatically use relative path if served via HTTP server, otherwise default to localhost:8000
export const API_BASE = window.location.protocol.startsWith('http') ? `${window.location.origin}/api` : "http://localhost:8000/api";

export const state = {
    currentClass: null,
    currentPage: 0,
    pageSize: 100,
    totalExports: 0,
    chunks: [],
    lengthDistribution: [],

    // File Explorer State
    currentFilePage: 0,
    filePageSize: 100,
    totalFiles: 0,
    fileSortField: 'size_bytes',
    fileSortOrder: 'desc',

    // Table Explorer State
    currentTable: null,
    currentTablePage: 0,
    tablePageSize: 100,
    totalTableRows: 0
};
