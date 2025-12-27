/**
 * Vanguard Mesh Viewer - Main Application
 * Modular Three.js viewer for Vanguard game assets
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// ============================================================================
// CONFIGURATION
// ============================================================================

const CONFIG = {
    defaultChunk: 'chunk_n25_26',
    meshPath: '../output/meshes/buildings/',
    terrainPath: '../output/terrain/terrain_grid/',
    flySpeed: 50000,
    cameraFar: 500000,
    gridSize: 200000,
};

// ============================================================================
// GLOBAL STATE
// ============================================================================

let scene, camera, renderer, controls;
let loadedModels = [];
let wireframeMode = false;
let gridVisible = true;
let deleteMode = false;
let flyMode = true;
let gridHelper;

const meshCache = new Map();
const deletedMeshNames = new Set();
let meshList = [];
let filteredMeshList = [];
let chunkList = [];
let filteredChunkList = [];
let chunkMeshRefs = new Set(); // Meshes referenced by current chunk
let currentChunk = null; // Currently loaded chunk name

const gltfLoader = new GLTFLoader();

// Fly mode state
const keys = { w: false, a: false, s: false, d: false, space: false, shift: false };
let yaw = 0, pitch = 0;
const euler = new THREE.Euler(0, 0, 0, 'YXZ');


// ============================================================================
// INITIALIZATION
// ============================================================================

function init() {
    // Scene
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1a2e);

    // Camera
    camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 1, CONFIG.cameraFar);
    camera.position.set(200, 200, 200);

    // Renderer
    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.shadowMap.enabled = true;
    document.getElementById('container').appendChild(renderer.domElement);

    // Controls
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.screenSpacePanning = true;
    controls.maxDistance = CONFIG.cameraFar;

    // Lighting
    setupLighting();

    // Grid
    gridHelper = new THREE.GridHelper(CONFIG.gridSize, 200, 0x444444, 0x222222);
    scene.add(gridHelper);

    // Event Listeners
    setupEventListeners();

    // Load mesh and BSP lists
    loadMeshList();
    loadChunkList();

    // Start render loop
    animate();

    console.log('Vanguard Mesh Viewer initialized');
}

function setupLighting() {
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
    scene.add(ambientLight);

    const directionalLight = new THREE.DirectionalLight(0xffffff, 1.0);
    directionalLight.position.set(500, 1000, 500);
    directionalLight.castShadow = true;
    directionalLight.shadow.mapSize.width = 2048;
    directionalLight.shadow.mapSize.height = 2048;
    scene.add(directionalLight);

    const sunLight = new THREE.DirectionalLight(0xfff9e8, 2.5);
    sunLight.position.set(0, 1000, 0);
    sunLight.castShadow = true;
    scene.add(sunLight);

    const fillLight = new THREE.DirectionalLight(0x8888ff, 0.3);
    fillLight.position.set(-200, 100, -200);
    scene.add(fillLight);
}

function setupEventListeners() {
    const container = document.getElementById('container');

    // Window resize
    window.addEventListener('resize', () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
    });

    // File input
    document.getElementById('file-input').addEventListener('change', handleFileInput);
    document.getElementById('file-input-add').addEventListener('change', handleFileInput);

    // Keyboard for fly mode
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keyup', handleKeyUp);

    // Click for delete mode OR pointer lock for fly mode
    renderer.domElement.addEventListener('click', (event) => {
        if (deleteMode) {
            handleDeleteClick(event);
        } else if (flyMode && !document.pointerLockElement) {
            // Request pointer lock when clicking canvas in fly mode
            container.requestPointerLock();
        }
    });

    // Mouse movement for fly mode look
    document.addEventListener('mousemove', handleMouseMove);

    // Pointer lock change handler
    document.addEventListener('pointerlockchange', () => {
        const isLocked = document.pointerLockElement === container;
        console.log('Pointer lock:', isLocked ? 'LOCKED' : 'UNLOCKED');
    });

    // Escape to exit pointer lock
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.pointerLockElement) {
            document.exitPointerLock();
        }
    });

    // Search inputs
    document.getElementById('mesh-search')?.addEventListener('input', filterMeshList);
    document.getElementById('chunk-search')?.addEventListener('input', filterChunkList);
    document.getElementById('chunk-only-toggle')?.addEventListener('change', applyMeshFilters);
}

function handleMouseMove(event) {
    // Only process when pointer is locked (fly mode active)
    if (!flyMode || !document.pointerLockElement) return;

    const movementX = Number.isFinite(event.movementX) ? event.movementX : 0;
    const movementY = Number.isFinite(event.movementY) ? event.movementY : 0;

    // Deadzone to filter tiny movements
    const deadzone = 0.5;
    const filteredX = Math.abs(movementX) < deadzone ? 0 : movementX;
    const filteredY = Math.abs(movementY) < deadzone ? 0 : movementY;

    if (filteredX === 0 && filteredY === 0) return;

    const sensitivity = 0.002;
    yaw -= filteredX * sensitivity;
    pitch -= filteredY * sensitivity;

    // Clamp pitch to prevent camera flipping
    pitch = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01, pitch));

    if (Number.isFinite(yaw) && Number.isFinite(pitch)) {
        euler.set(pitch, yaw, 0);
        camera.quaternion.setFromEuler(euler);
    }
}


// ============================================================================
// RENDER LOOP
// ============================================================================

function animate() {
    requestAnimationFrame(animate);

    if (flyMode) {
        updateFlyMode();
    } else {
        controls.update();
    }

    renderer.render(scene, camera);
}

function updateFlyMode() {
    const speed = CONFIG.flySpeed * 0.016; // 60fps normalized
    const direction = new THREE.Vector3();

    if (keys.w) direction.z -= 1;
    if (keys.s) direction.z += 1;
    if (keys.a) direction.x -= 1;
    if (keys.d) direction.x += 1;
    if (keys.space) direction.y += 1;
    if (keys.shift) direction.y -= 1;

    if (direction.length() > 0) {
        direction.normalize().multiplyScalar(speed);
        direction.applyQuaternion(camera.quaternion);
        camera.position.add(direction);
        controls.target.copy(camera.position).add(new THREE.Vector3(0, 0, -100).applyQuaternion(camera.quaternion));
    }
}

// ============================================================================
// EVENT HANDLERS
// ============================================================================

function handleFileInput(event) {
    const files = event.target.files;
    for (const file of files) {
        loadModel(file);
    }
}

function handleKeyDown(event) {
    const key = event.key.toLowerCase();
    if (key in keys) keys[key] = true;
    if (key === ' ') { keys.space = true; event.preventDefault(); }
    if (key === 'f') toggleFlyMode();
}

function handleKeyUp(event) {
    const key = event.key.toLowerCase();
    if (key in keys) keys[key] = false;
    if (key === ' ') keys.space = false;
}

function handleDeleteClick(event) {
    if (!deleteMode) return;

    const rect = renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1
    );

    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(mouse, camera);

    const allMeshes = [];
    loadedModels.forEach(model => {
        model.traverse(child => {
            if (child.isMesh) allMeshes.push(child);
        });
    });

    const intersects = raycaster.intersectObjects(allMeshes, false);

    if (intersects.length > 0) {
        const hitMesh = intersects[0].object;

        // Find the actor node (child of the top-level scene/model)
        let target = hitMesh;

        // Traverse up to find the container node that has the mesh_ref metadata
        // This handles cases where we hit a child mesh instance or the marker cube
        let foundContainer = null;
        let curr = hitMesh;

        while (curr && curr !== scene) {
            // Check if this node is the one with the metadata (the Actor Node from objects.gltf)
            if (curr.userData && (curr.userData.mesh_ref || curr.userData.is_prefab)) {
                foundContainer = curr;
                break;
            }

            // If we hit a root loaded model (the Scene root), we stop just below it
            if (loadedModels.includes(curr.parent)) {
                // The current node is a direct child of a loaded root (e.g. an object node)
                // This is likely what we want if we didn't find metadata yet
                if (!foundContainer) foundContainer = curr;
                break;
            }

            curr = curr.parent;
        }

        if (foundContainer) {
            target = foundContainer;
        } else {
            // Fallback to original behavior if traverse failed to find specific container
            while (target.parent && target.parent !== scene && !loadedModels.includes(target)) {
                target = target.parent;
            }
        }


        const meshName = target.userData?.mesh_ref || target.name || hitMesh.name || 'unnamed';
        console.log('--- DELETING MESH ---');
        console.log('Target Name:', target.name);
        console.log('Mesh Ref:', target.userData?.mesh_ref);

        deletedMeshNames.add(meshName);
        console.log('Current Deleted List:', Array.from(deletedMeshNames));

        // Remove from scene or parent
        if (target.parent) {
            target.parent.remove(target);
        }

        // Dispose resources
        target.traverse(child => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach(m => m.dispose());
            }
        });

        updateStats();
        console.log(`Successfully deleted: ${meshName}. Total deleted meshes: ${deletedMeshNames.size}`);
    }
}


// ============================================================================
// MODEL LOADING
// ============================================================================

function loadModel(fileOrUrl, isUrl = false) {
    const loading = document.getElementById('loading');
    loading.style.display = 'block';
    loading.textContent = `Loading ${isUrl ? fileOrUrl : fileOrUrl.name}...`;

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
                    mat.wireframe = wireframeMode;
                });
            }

            // Resolve mesh references (used by objects.gltf)
            if (child.userData && child.userData.mesh_ref) {
                resolveAndLoadMesh(child);
            }
        });

        scene.add(model);
        loadedModels.push(model);
        updateStats();
        loading.style.display = 'none';

        console.log('Loaded:', model.name);
    };

    const errorCallback = (error) => {
        console.error('Load error:', error);
        loading.style.display = 'none';
    };

    if (isUrl) {
        gltfLoader.load(fileOrUrl, loadCallback, undefined, errorCallback);
    } else {
        const url = URL.createObjectURL(fileOrUrl);
        gltfLoader.load(url, (gltf) => {
            URL.revokeObjectURL(url);
            loadCallback(gltf);
        }, undefined, errorCallback);
    }
}

// Resolve mesh_ref from objects.gltf and load actual mesh
async function resolveAndLoadMesh(node) {
    const meshRef = node.userData.mesh_ref;
    if (!meshRef || deletedMeshNames.has(meshRef)) return;

    // Find best match in meshList
    // Manifest contains paths like "PackageName/MeshName.gltf"
    // mesh_ref is typically just "MeshName"
    const ref = meshRef.toLowerCase();
    let bestMatch = null;

    // 1. Look for mesh name at end of path (handles subdirectory structure)
    bestMatch = meshList.find(m => {
        const mLower = m.toLowerCase();
        const fileName = mLower.split('/').pop().replace('.gltf', '');
        return fileName === ref;
    });

    // 2. Try with LOD suffixes
    if (!bestMatch) {
        const lodSuffixes = ['_L0', '_L1', '_L2', '_ver01'];
        for (const suffix of lodSuffixes) {
            const candidate = ref + suffix.toLowerCase();
            bestMatch = meshList.find(m => {
                const mLower = m.toLowerCase();
                const fileName = mLower.split('/').pop().replace('.gltf', '');
                return fileName === candidate;
            });
            if (bestMatch) break;
        }
    }

    // 3. Fuzzy match - mesh name contained in the full path
    if (!bestMatch) {
        bestMatch = meshList.find(m => m.toLowerCase().includes('/' + ref + '.gltf'));
    }

    if (!bestMatch) {
        // No match found, can't resolve
        return;
    }

    // Check cache - add as child of the node (not to scene directly)
    if (meshCache.has(bestMatch)) {
        const cached = meshCache.get(bestMatch);
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

        loadedModels.push(instance);
        updateStats();
        return;
    }


    // Load the mesh
    const path = CONFIG.meshPath + bestMatch;
    try {
        const res = await fetch(path, { method: 'HEAD' });
        if (!res.ok) return;

        gltfLoader.load(path, (gltf) => {
            const meshModel = gltf.scene;
            meshModel.name = bestMatch;

            // Cache the original model
            meshCache.set(bestMatch, meshModel.clone());

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

            loadedModels.push(meshModel);
            updateStats();

            console.log(`Resolved mesh: ${meshRef} -> ${bestMatch}`);
        }, undefined, (err) => {
            console.warn(`Failed to parse glTF for ${bestMatch}:`, err);
        });
    } catch (e) {
        console.warn(`Failed to fetch mesh: ${meshRef}`);
    }
}



// ============================================================================
// GLOBAL FUNCTIONS (exposed to window for button onclick)
// ============================================================================

window.resetCamera = function () {
    camera.position.set(200, 200, 200);
    controls.target.set(0, 0, 0);
    controls.update();
};

window.toggleWireframe = function () {
    wireframeMode = !wireframeMode;
    loadedModels.forEach(model => {
        model.traverse(child => {
            if (child.isMesh && child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach(mat => { mat.wireframe = wireframeMode; });
            }
        });
    });
};

window.toggleGrid = function () {
    gridVisible = !gridVisible;
    gridHelper.visible = gridVisible;
};

window.clearModels = function () {
    loadedModels.forEach(model => {
        scene.remove(model);
        model.traverse(child => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach(m => m.dispose());
            }
        });
    });
    loadedModels = [];
    updateStats();
};

window.toggleFlyMode = function () {
    flyMode = !flyMode;
    const btn = document.getElementById('fly-mode-btn');
    if (btn) {
        btn.textContent = flyMode ? 'Fly Mode ON' : 'Fly Mode';
        btn.classList.toggle('active', flyMode);
    }
    controls.enabled = !flyMode;
};

window.toggleDeleteMode = function () {
    deleteMode = !deleteMode;
    const btn = document.getElementById('delete-mode-btn');
    if (btn) {
        btn.textContent = deleteMode ? 'DELETE MODE ON' : 'Delete Mode';
        btn.classList.toggle('active', deleteMode);
    }
    document.body.classList.toggle('delete-mode', deleteMode);
    console.log('Delete mode:', deleteMode ? 'ON' : 'OFF');
};

window.loadChunkMeshes = async function () {
    if (chunkMeshRefs.size === 0) {
        const msg = currentChunk
            ? `Chunk "${currentChunk}" has no object data (terrain only).`
            : 'No chunk loaded. Load a chunk from the Chunk Browser first.';
        alert(msg);
        console.warn(msg);
        return;
    }

    const loading = document.getElementById('loading');
    loading.style.display = 'block';

    let loaded = 0;
    const total = chunkMeshRefs.size;

    for (const meshRef of chunkMeshRefs) {
        loading.textContent = `Loading chunk meshes... ${loaded}/${total}`;

        // Find matching mesh in meshList
        const match = meshList.find(m => {
            const fileName = m.split('/').pop().replace('.gltf', '').toLowerCase();
            return fileName === meshRef;
        });

        if (match) {
            await loadMeshAsync(match);
            loaded++;
        }
    }

    loading.style.display = 'none';
    console.log(`Loaded ${loaded} chunk meshes`);
};

// Helper for async mesh loading
async function loadMeshAsync(meshPath) {
    return new Promise((resolve) => {
        const path = CONFIG.meshPath + meshPath;
        gltfLoader.load(path, (gltf) => {
            const model = gltf.scene;
            model.name = meshPath;

            model.traverse((child) => {
                if (child.isMesh && child.material) {
                    const mats = Array.isArray(child.material) ? child.material : [child.material];
                    mats.forEach((mat) => {
                        mat.side = THREE.DoubleSide;
                        mat.wireframe = wireframeMode;
                    });
                }
            });

            // Apply Z-up to Y-up rotation
            model.rotation.x = -Math.PI / 2;

            scene.add(model);
            loadedModels.push(model);
            updateStats();
            resolve();
        }, undefined, () => resolve());
    });
}

// Load a specific chunk's terrain
window.loadChunk = async function (chunkName) {
    const loading = document.getElementById('loading');
    loading.style.display = 'block';
    loading.textContent = `Loading ${chunkName}...`;

    window.clearModels();
    currentChunk = chunkName;
    updateChunkListUI(); // Update selection highlight

    const timestamp = Date.now();
    const objectsUrl = `${CONFIG.terrainPath}${chunkName}_objects.gltf?t=${timestamp}`;

    // Fetch objects.gltf to extract mesh_refs for chunk filtering
    chunkMeshRefs.clear();
    try {
        const objResponse = await fetch(objectsUrl);
        if (objResponse.ok) {
            const objData = await objResponse.json();
            for (const node of (objData.nodes || [])) {
                if (node.extras && node.extras.mesh_ref) {
                    chunkMeshRefs.add(node.extras.mesh_ref.toLowerCase());
                }
            }
            console.log(`Loaded ${chunkMeshRefs.size} mesh refs from ${chunkName}`);
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

    loading.style.display = 'none';
};

window.loadDefaults = async function () {
    const loading = document.getElementById('loading');
    loading.style.display = 'block';
    loading.textContent = 'Loading default chunk...';

    window.clearModels();

    const timestamp = Date.now();
    const objectsUrl = `${CONFIG.terrainPath}${CONFIG.defaultChunk}_objects.gltf?t=${timestamp}`;

    // Fetch objects.gltf to extract mesh_refs for chunk filtering
    try {
        const objResponse = await fetch(objectsUrl);
        if (objResponse.ok) {
            const objData = await objResponse.json();
            chunkMeshRefs.clear();
            for (const node of (objData.nodes || [])) {
                if (node.extras && node.extras.mesh_ref) {
                    chunkMeshRefs.add(node.extras.mesh_ref.toLowerCase());
                }
            }
            console.log(`Loaded ${chunkMeshRefs.size} mesh refs from chunk`);
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

    loading.style.display = 'none';
};

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function updateStats() {
    let vertices = 0, triangles = 0, meshes = 0, textures = new Set();

    // Use scene traverse for accurate viewport stats, excluding hidden placeholders
    scene.traverse(child => {
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

    document.getElementById('stat-vertices').textContent = vertices.toLocaleString();
    document.getElementById('stat-triangles').textContent = Math.floor(triangles).toLocaleString();
    document.getElementById('stat-meshes').textContent = meshes.toLocaleString();
    document.getElementById('stat-textures').textContent = textures.size.toLocaleString();
}


async function loadMeshList() {
    try {
        const response = await fetch(CONFIG.meshPath + 'manifest.json?t=' + Date.now());
        if (response.ok) {
            const manifest = await response.json();
            // Handle both array format and {files: [...]} format
            meshList = Array.isArray(manifest) ? manifest : (manifest.files || []);
            filteredMeshList = [...meshList];
            document.getElementById('mesh-count').textContent = meshList.length;
            updateMeshListUI();
        }
    } catch (error) {
        console.error('Failed to load mesh list:', error);
    }
}

async function loadChunkList() {
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
            chunkList = [...new Set(chunks)].sort();
            filteredChunkList = [...chunkList];
            document.getElementById('chunk-count').textContent = chunkList.length;
            updateChunkListUI();
        }
    } catch (error) {
        console.error('Failed to load chunk list:', error);
    }
}

function filterChunkList(event) {
    const query = event.target.value.toLowerCase();
    filteredChunkList = query ? chunkList.filter(c => c.toLowerCase().includes(query)) : [...chunkList];
    updateChunkListUI();
}

function updateChunkListUI() {
    const container = document.getElementById('chunk-list-container');
    if (!container) return;

    container.innerHTML = filteredChunkList.map((chunk) =>
        `<div class="chunk-list-item${chunk === currentChunk ? ' selected' : ''}" onclick="loadChunk('${chunk}')">${chunk}</div>`
    ).join('');
}

function filterMeshList(event) {
    applyMeshFilters();
}

function applyMeshFilters() {
    const searchEl = document.getElementById('mesh-search');
    const chunkOnlyEl = document.getElementById('chunk-only-toggle');

    const query = searchEl ? searchEl.value.toLowerCase() : '';
    const chunkOnly = chunkOnlyEl ? chunkOnlyEl.checked : false;

    // Start with full list
    let result = [...meshList];

    // Apply chunk filter if checked
    if (chunkOnly && chunkMeshRefs.size > 0) {
        result = result.filter(m => {
            const meshName = m.split('/').pop().replace('.gltf', '').toLowerCase();
            return chunkMeshRefs.has(meshName);
        });
    }

    // Apply search filter
    if (query) {
        result = result.filter(m => m.toLowerCase().includes(query));
    }

    // Sort by path length descending (larger/more complex meshes tend to have longer names)
    result.sort((a, b) => b.length - a.length);

    filteredMeshList = result;
    updateMeshListUI();
}

function updateMeshListUI() {
    const container = document.getElementById('mesh-list-container');
    if (!container) return;

    container.innerHTML = filteredMeshList.slice(0, 100).map((mesh, i) =>
        `<div class="mesh-list-item" onclick="loadMesh('${mesh}')">${mesh}</div>`
    ).join('');
}

window.loadMesh = function (meshPath) {
    const fullPath = CONFIG.meshPath + meshPath + '?t=' + Date.now();
    loadModel(fullPath, true);
};

window.loadBsp = function (bspFile) {
    const fullPath = CONFIG.terrainPath + bspFile + '?t=' + Date.now();
    loadModel(fullPath, true);
};

// ============================================================================
// START
// ============================================================================

init();
