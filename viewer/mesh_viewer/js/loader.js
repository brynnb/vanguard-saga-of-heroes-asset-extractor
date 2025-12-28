import * as THREE from 'three';
import { state } from './state.js';
import { CONFIG } from './config.js';
import { updateStats, updateChunkListUI, applyMeshFilters } from './ui.js';

export function loadModel(fileOrUrl, isUrl = false) {
    const loading = document.getElementById('loading');
    if (loading) {
        loading.style.display = 'block';
        loading.textContent = `Loading ${isUrl ? fileOrUrl : fileOrUrl.name}...`;
    }

    const loadCallback = (gltf) => {
        const model = gltf.scene;
        const fileName = isUrl ? fileOrUrl.split('/').pop().split('?')[0].replace('.gltf', '') : fileOrUrl.name.replace('.gltf', '');
        model.name = fileName;

        // Process all nodes for material setup and mesh references
        model.traverse((child) => {
            // Set double-sided materials
            if (child.isMesh && child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach((mat) => {
                    mat.side = THREE.DoubleSide;
                    mat.wireframe = state.wireframeMode;
                });
            }

            // Resolve mesh references (used by objects.gltf)
            if (child.userData && child.userData.mesh_ref) {
                resolveAndLoadMesh(child);
            }
        });

        if (state.scene) state.scene.add(model);
        state.loadedModels.push(model);
        updateStats();
        if (loading) loading.style.display = 'none';

        console.log('Loaded:', model.name);
    };

    const errorCallback = (error) => {
        console.error('Load error:', error);
        if (loading) loading.style.display = 'none';
    };

    if (isUrl) {
        state.gltfLoader.load(fileOrUrl, loadCallback, undefined, errorCallback);
    } else {
        const url = URL.createObjectURL(fileOrUrl);
        state.gltfLoader.load(url, (gltf) => {
            URL.revokeObjectURL(url);
            loadCallback(gltf);
        }, undefined, errorCallback);
    }
}

// Resolve mesh_ref from objects.gltf and load actual mesh
export async function resolveAndLoadMesh(node) {
    const meshRef = node.userData.mesh_ref;
    if (!meshRef || state.deletedMeshNames.has(meshRef)) return;

    // Find best match in meshList
    // Manifest contains paths like "PackageName/MeshName.gltf"
    // mesh_ref is typically just "MeshName"
    const ref = meshRef.toLowerCase();
    let bestMatch = null;

    // 1. Look for mesh name at end of path (handles subdirectory structure)
    bestMatch = state.meshList.find(m => {
        const mLower = m.toLowerCase();
        const fileName = mLower.split('/').pop().replace('.gltf', '');
        return fileName === ref;
    });

    // 2. Try with LOD suffixes
    if (!bestMatch) {
        const lodSuffixes = ['_L0', '_L1', '_L2', '_ver01'];
        for (const suffix of lodSuffixes) {
            const candidate = ref + suffix.toLowerCase();
            bestMatch = state.meshList.find(m => {
                const mLower = m.toLowerCase();
                const fileName = mLower.split('/').pop().replace('.gltf', '');
                return fileName === candidate;
            });
            if (bestMatch) break;
        }
    }

    // 3. Fuzzy match - mesh name contained in the full path
    if (!bestMatch) {
        bestMatch = state.meshList.find(m => m.toLowerCase().includes('/' + ref + '.gltf'));
    }

    if (!bestMatch) {
        // No match found, can't resolve
        return;
    }

    // Check cache - add as child of the node (not to scene directly)
    if (state.meshCache.has(bestMatch)) {
        const cached = state.meshCache.get(bestMatch);
        const instance = cached.clone();

        // Reset position relative to parent actor node
        instance.position.set(0, 0, 0);

        // Apply Z-up to Y-up rotation (most meshes still in old format)
        instance.rotation.x = -Math.PI / 2;

        // Add as child of node (inherits node's world transform)
        node.add(instance);

        // Hide the placeholder marker cube if it's there
        node.traverse(c => {
            if (c.isMesh && c !== instance && c.name === "MarkerCube") c.visible = false;
        });

        state.loadedModels.push(instance);
        updateStats();
        return;
    }


    // Load the mesh
    const path = CONFIG.meshPath + bestMatch;
    try {
        const res = await fetch(path, { method: 'HEAD' });
        if (!res.ok) return;

        state.gltfLoader.load(path, (gltf) => {
            const meshModel = gltf.scene;
            meshModel.name = bestMatch;

            // Cache the original model
            state.meshCache.set(bestMatch, meshModel.clone());

            // Reset position relative to parent actor node
            meshModel.position.set(0, 0, 0);

            // Apply Z-up to Y-up rotation (most meshes still in old format)
            meshModel.rotation.x = -Math.PI / 2;

            // Add as child of node (inherits node's world transform)
            node.add(meshModel);


            // Hide the placeholder marker cube
            node.traverse(c => {
                if (c.isMesh && c !== meshModel && c.name === "MarkerCube") c.visible = false;
            });

            state.loadedModels.push(meshModel);
            updateStats();

            console.log(`Resolved mesh: ${meshRef} -> ${bestMatch}`);
        }, undefined, (err) => {
            console.warn(`Failed to parse glTF for ${bestMatch}:`, err);
        });
    } catch (e) {
        console.warn(`Failed to fetch mesh: ${meshRef}`);
    }
}

export function clearModels() {
    state.loadedModels.forEach(model => {
        if (state.scene) state.scene.remove(model);
        model.traverse(child => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach(m => m.dispose());
            }
        });
    });
    state.loadedModels = [];
    updateStats();
}

export async function loadChunkMeshes() {
    if (state.chunkMeshRefs.size === 0) {
        const msg = state.currentChunk
            ? `Chunk "${state.currentChunk}" has no object data (terrain only).`
            : 'No chunk loaded. Load a chunk from the Chunk Browser first.';
        alert(msg);
        console.warn(msg);
        return;
    }

    const loading = document.getElementById('loading');
    if (loading) loading.style.display = 'block';

    let loaded = 0;
    const total = state.chunkMeshRefs.size;

    for (const meshRef of state.chunkMeshRefs) {
        if (loading) loading.textContent = `Loading chunk meshes... ${loaded}/${total}`;

        // Find matching mesh in meshList
        const match = state.meshList.find(m => {
            const fileName = m.split('/').pop().replace('.gltf', '').toLowerCase();
            return fileName === meshRef;
        });

        if (match) {
            await loadMeshAsync(match);
            loaded++;
        }
    }

    if (loading) loading.style.display = 'none';
    console.log(`Loaded ${loaded} chunk meshes`);
}

// Helper for async mesh loading
async function loadMeshAsync(meshPath) {
    return new Promise((resolve) => {
        const path = CONFIG.meshPath + meshPath;
        state.gltfLoader.load(path, (gltf) => {
            const model = gltf.scene;
            model.name = meshPath;

            model.traverse((child) => {
                if (child.isMesh && child.material) {
                    const mats = Array.isArray(child.material) ? child.material : [child.material];
                    mats.forEach((mat) => {
                        mat.side = THREE.DoubleSide;
                        mat.wireframe = state.wireframeMode;
                    });
                }
            });

            // Apply Z-up to Y-up rotation
            model.rotation.x = -Math.PI / 2;

            if (state.scene) state.scene.add(model);
            state.loadedModels.push(model);
            updateStats();
            resolve();
        }, undefined, () => resolve());
    });
}

export async function loadChunk(chunkName) {
    const loading = document.getElementById('loading');
    if (loading) {
        loading.style.display = 'block';
        loading.textContent = `Loading ${chunkName}...`;
    }

    clearModels();
    state.currentChunk = chunkName;
    updateChunkListUI(); // Update selection highlight

    const timestamp = Date.now();
    const objectsUrl = `${CONFIG.terrainPath}${chunkName}_objects.gltf?t=${timestamp}`;

    // Fetch objects.gltf to extract mesh_refs for chunk filtering
    state.chunkMeshRefs.clear();
    try {
        const objResponse = await fetch(objectsUrl);
        if (objResponse.ok) {
            const objData = await objResponse.json();
            for (const node of (objData.nodes || [])) {
                if (node.extras && node.extras.mesh_ref) {
                    state.chunkMeshRefs.add(node.extras.mesh_ref.toLowerCase());
                }
            }
            console.log(`Loaded ${state.chunkMeshRefs.size} mesh refs from ${chunkName}`);
        } else {
            console.log(`No objects.gltf for ${chunkName} (terrain only)`);
        }
        applyMeshFilters();
    } catch (e) {
        console.warn('Failed to extract mesh refs:', e);
    }

    // Load terrain and objects
    const defaultFiles = [
        `${CONFIG.terrainPath}${chunkName}_terrain.gltf?t=${timestamp}`,
        objectsUrl,
    ];

    for (const filePath of defaultFiles) {
        try {
            const response = await fetch(filePath);
            if (!response.ok) {
                console.warn(`Failed to load ${filePath}: ${response.status}`);
                continue;
            }
            loadModel(filePath, true);
        } catch (error) {
            console.error(`Error loading ${filePath}:`, error);
        }
    }

    if (loading) loading.style.display = 'none';
}

export async function loadDefaults() {
    const loading = document.getElementById('loading');
    if (loading) {
        loading.style.display = 'block';
        loading.textContent = 'Loading default chunk...';
    }

    clearModels();

    const timestamp = Date.now();
    const objectsUrl = `${CONFIG.terrainPath}${CONFIG.defaultChunk}_objects.gltf?t=${timestamp}`;

    // Fetch objects.gltf to extract mesh_refs for chunk filtering
    try {
        const objResponse = await fetch(objectsUrl);
        if (objResponse.ok) {
            const objData = await objResponse.json();
            state.chunkMeshRefs.clear();
            for (const node of (objData.nodes || [])) {
                if (node.extras && node.extras.mesh_ref) {
                    state.chunkMeshRefs.add(node.extras.mesh_ref.toLowerCase());
                }
            }
            console.log(`Loaded ${state.chunkMeshRefs.size} mesh refs from chunk`);
            applyMeshFilters(); // Update mesh browser with chunk filter
        }
    } catch (e) {
        console.warn('Failed to extract mesh refs:', e);
    }

    const defaultFiles = [
        `${CONFIG.terrainPath}${CONFIG.defaultChunk}_terrain.gltf?t=${timestamp}`,
        `${CONFIG.terrainPath}${CONFIG.defaultChunk}_bsp.gltf?t=${timestamp}`,
        objectsUrl,
    ];

    for (const filePath of defaultFiles) {
        try {
            const response = await fetch(filePath);
            if (!response.ok) {
                console.warn(`Failed to load ${filePath}: ${response.status}`);
                continue;
            }
            loadModel(filePath, true);
        } catch (error) {
            console.error(`Error loading ${filePath}:`, error);
        }
    }

    if (loading) loading.style.display = 'none';
}

export function loadMesh(meshPath) {
    const fullPath = CONFIG.meshPath + meshPath + '?t=' + Date.now();
    loadModel(fullPath, true);
}

export function loadBsp(bspFile) {
    const fullPath = CONFIG.terrainPath + bspFile + '?t=' + Date.now();
    loadModel(fullPath, true);
}
