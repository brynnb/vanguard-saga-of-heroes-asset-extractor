/**
 * Vanguard Mesh Viewer - Main Application
 * Modular Three.js viewer for Vanguard game assets
 */

import { init, resetCamera, toggleWireframe, toggleGrid } from './core.js';
import { toggleFlyMode, toggleDeleteMode } from './input.js';
import { loadChunkMeshes, loadChunk, loadDefaults, loadMesh, loadBsp, clearModels } from './loader.js';

// ============================================================================
// GLOBAL FUNCTIONS (exposed to window for button onclick)
// ============================================================================

window.resetCamera = resetCamera;
window.toggleWireframe = toggleWireframe;
window.toggleGrid = toggleGrid;
window.clearModels = clearModels;
window.toggleFlyMode = toggleFlyMode;
window.toggleDeleteMode = toggleDeleteMode;
window.loadChunkMeshes = loadChunkMeshes;
window.loadChunk = loadChunk;
window.loadDefaults = loadDefaults;
window.loadMesh = loadMesh;
window.loadBsp = loadBsp;

// ============================================================================
// START
// ============================================================================

init();
