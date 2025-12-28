import { state } from './state.js';
import { CONFIG } from './config.js';

export function updateStats() {
    let vertices = 0, triangles = 0, meshes = 0, textures = new Set();

    if (!state.scene) return;

    // Use scene traverse for accurate viewport stats, excluding hidden placeholders
    state.scene.traverse(child => {
        if (child.isMesh && child.visible) {
            // Count actual visible geometry, excluding marker cubes
            const isMarker = child.name === "MarkerCube" || (child.parent && child.parent.name === "MarkerCube");
            if (!isMarker) {
                meshes++;
                if (child.geometry) {
                    vertices += child.geometry.attributes.position?.count || 0;
                    triangles += child.geometry.index ? child.geometry.index.count / 3 :
                        (child.geometry.attributes.position?.count || 0) / 3;
                }
                if (child.material?.map) textures.add(child.material.map);
            }
        }
    });

    const elV = document.getElementById('stat-vertices');
    if (elV) elV.textContent = vertices.toLocaleString();

    const elT = document.getElementById('stat-triangles');
    if (elT) elT.textContent = Math.floor(triangles).toLocaleString();

    const elM = document.getElementById('stat-meshes');
    if (elM) elM.textContent = meshes.toLocaleString();

    const elTex = document.getElementById('stat-textures');
    if (elTex) elTex.textContent = textures.size.toLocaleString();
}

export async function loadMeshList() {
    try {
        const response = await fetch(CONFIG.meshPath);
        if (response.ok) {
            const html = await response.text();
            // Match .gltf files to get mesh names
            const matches = html.match(/href="([^"]+\.gltf)"/g) || [];
            const meshes = matches.map(m => m.match(/href="([^"]+)"/)[1]);

            state.meshList = [...new Set(meshes)].sort();
            state.filteredMeshList = [...state.meshList];

            const countEl = document.getElementById('mesh-count');
            if (countEl) countEl.textContent = state.meshList.length;

            updateMeshListUI();
        }
    } catch (error) {
        console.error('Failed to load mesh list:', error);
    }
}

export async function loadChunkList() {
    try {
        const response = await fetch(CONFIG.terrainPath);
        if (response.ok) {
            const html = await response.text();
            // Match terrain.gltf files to get chunk names
            const matches = html.match(/href="([^"]+_terrain\.gltf)"/g) || [];
            const chunks = matches.map(m => {
                const file = m.match(/href="([^"]+)"/)[1];
                return file.replace('_terrain.gltf', '');
            });
            state.chunkList = [...new Set(chunks)].sort();
            state.filteredChunkList = [...state.chunkList];

            const countEl = document.getElementById('chunk-count');
            if (countEl) countEl.textContent = state.chunkList.length;

            updateChunkListUI();
        }
    } catch (error) {
        console.error('Failed to load chunk list:', error);
    }
}

export function filterChunkList(event) {
    const query = event.target.value.toLowerCase();
    state.filteredChunkList = query ? state.chunkList.filter(c => c.toLowerCase().includes(query)) : [...state.chunkList];
    updateChunkListUI();
}

export function updateChunkListUI() {
    const container = document.getElementById('chunk-list-container');
    if (!container) return;

    container.innerHTML = state.filteredChunkList.map((chunk) =>
        `<div class="chunk-list-item${chunk === state.currentChunk ? ' selected' : ''}" onclick="window.loadChunk('${chunk}')">${chunk}</div>`
    ).join('');
}

export function filterMeshList(event) {
    applyMeshFilters();
}

export function applyMeshFilters() {
    const searchEl = document.getElementById('mesh-search');
    const chunkOnlyEl = document.getElementById('chunk-only-toggle');

    const query = searchEl ? searchEl.value.toLowerCase() : '';
    const chunkOnly = chunkOnlyEl ? chunkOnlyEl.checked : false;

    // Start with full list
    let result = [...state.meshList];

    // Apply chunk filter if checked
    if (chunkOnly && state.chunkMeshRefs.size > 0) {
        result = result.filter(m => {
            const meshName = m.split('/').pop().replace('.gltf', '').toLowerCase();
            return state.chunkMeshRefs.has(meshName);
        });
    }

    // Apply search filter
    if (query) {
        result = result.filter(m => m.toLowerCase().includes(query));
    }

    // Sort by path length descending (larger/more complex meshes tend to have longer names)
    result.sort((a, b) => b.length - a.length);

    state.filteredMeshList = result;
    updateMeshListUI();
}

export function updateMeshListUI() {
    const container = document.getElementById('mesh-list-container');
    if (!container) return;

    container.innerHTML = state.filteredMeshList.slice(0, 100).map((mesh, i) =>
        `<div class="mesh-list-item" onclick="window.loadMesh('${mesh}')">${mesh}</div>`
    ).join('');
}
