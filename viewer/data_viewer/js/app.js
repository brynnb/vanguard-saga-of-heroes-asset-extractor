/* Data Viewer Entry Point */

import {
    loadClasses, loadChunks, updateStats,
    selectClass, loadExports, prevPage, nextPage,
    clearFilters, switchTab, showPropertyValues,
    showClassView
} from './views/classes.js';

import {
    showFilesView, loadFiles, sortFiles,
    prevFilesPage, nextFilesPage, clearFileFilters
} from './views/files.js';

import {
    showTableView, loadTableCounts, loadTableData,
    prevTablePage, nextTablePage, clearTableSearch,
    runQuery, setQuery
} from './views/tables.js';

import {
    showFileStructure, closeStructureModal
} from './views/modal.js';

import {
    showExportInPanel, closeDetailPanel
} from './components/exportPanel.js';

// --- Expose functions to window for HTML event handlers ---

// Class View
window.selectClass = selectClass;
window.loadExports = loadExports; // For filter usage
window.prevPage = prevPage;
window.nextPage = nextPage;
window.clearFilters = clearFilters;
window.switchTab = switchTab;
window.showPropertyValues = showPropertyValues;
window.showClassView = showClassView;

// File View
window.showFilesView = showFilesView;
window.loadFiles = loadFiles; // For filter usage
window.sortFiles = sortFiles;
window.prevFilesPage = prevFilesPage;
window.nextFilesPage = nextFilesPage;
window.clearFileFilters = clearFileFilters;

// Table View
window.showTableView = showTableView;
window.loadTableData = loadTableData; // For filter usage
window.prevTablePage = prevTablePage;
window.nextTablePage = nextTablePage;
window.clearTableSearch = clearTableSearch;
window.runQuery = runQuery;
window.setQuery = setQuery;

// Modal
window.showFileStructure = showFileStructure;
window.closeStructureModal = closeStructureModal;

// Export Panel
window.showExportInPanel = showExportInPanel;
window.closeDetailPanel = closeDetailPanel;

// --- Initialization ---

async function init() {
    await loadClasses();
    await loadChunks();
    updateStats();
    loadTableCounts();
}

// Start app
init();
