import * as THREE from 'three';
import { state } from './state.js';
import { CONFIG } from './config.js';
import { loadModel } from './loader.js';
import { updateStats, filterMeshList, filterChunkList, applyMeshFilters } from './ui.js';

export function setupEventListeners() {
    const container = document.getElementById('container');

    // Window resize
    window.addEventListener('resize', () => {
        if (state.camera && state.renderer) {
            state.camera.aspect = window.innerWidth / window.innerHeight;
            state.camera.updateProjectionMatrix();
            state.renderer.setSize(window.innerWidth, window.innerHeight);
        }
    });

    // File input
    const fileInput = document.getElementById('file-input');
    if (fileInput) fileInput.addEventListener('change', handleFileInput);

    const fileInputAdd = document.getElementById('file-input-add');
    if (fileInputAdd) fileInputAdd.addEventListener('change', handleFileInput);

    // Keyboard for fly mode
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('keyup', handleKeyUp);

    // Click for delete mode OR pointer lock for fly mode
    if (state.renderer) {
        state.renderer.domElement.addEventListener('click', (event) => {
            if (state.deleteMode) {
                handleDeleteClick(event);
            } else if (state.flyMode && !document.pointerLockElement) {
                // Request pointer lock when clicking canvas in fly mode
                container.requestPointerLock();
            }
        });
    }

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
    const meshSearch = document.getElementById('mesh-search');
    if (meshSearch) meshSearch.addEventListener('input', filterMeshList);

    const chunkSearch = document.getElementById('chunk-search');
    if (chunkSearch) chunkSearch.addEventListener('input', filterChunkList);

    const chunkToggle = document.getElementById('chunk-only-toggle');
    if (chunkToggle) chunkToggle.addEventListener('change', applyMeshFilters);
}

export function handleFileInput(event) {
    const files = event.target.files;
    for (const file of files) {
        loadModel(file);
    }
}

export function handleKeyDown(event) {
    const key = event.key.toLowerCase();
    if (key in state.keys) state.keys[key] = true;
    if (key === ' ') { state.keys.space = true; event.preventDefault(); }
    if (key === 'f') toggleFlyMode();
}

export function handleKeyUp(event) {
    const key = event.key.toLowerCase();
    if (key in state.keys) state.keys[key] = false;
    if (key === ' ') state.keys.space = false;
}

export function handleMouseMove(event) {
    // Only process when pointer is locked (fly mode active)
    if (!state.flyMode || !document.pointerLockElement) return;

    const movementX = Number.isFinite(event.movementX) ? event.movementX : 0;
    const movementY = Number.isFinite(event.movementY) ? event.movementY : 0;

    // Deadzone to filter tiny movements
    const deadzone = 0.5;
    const filteredX = Math.abs(movementX) < deadzone ? 0 : movementX;
    const filteredY = Math.abs(movementY) < deadzone ? 0 : movementY;

    if (filteredX === 0 && filteredY === 0) return;

    const sensitivity = 0.002;
    state.yaw -= filteredX * sensitivity;
    state.pitch -= filteredY * sensitivity;

    // Clamp pitch to prevent camera flipping
    state.pitch = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01, state.pitch));

    if (Number.isFinite(state.yaw) && Number.isFinite(state.pitch)) {
        state.euler.set(state.pitch, state.yaw, 0);
        state.camera.quaternion.setFromEuler(state.euler);
    }
}

export function updateFlyMode() {
    const speed = CONFIG.flySpeed * 0.016; // 60fps normalized
    const direction = new THREE.Vector3();

    if (state.keys.w) direction.z -= 1;
    if (state.keys.s) direction.z += 1;
    if (state.keys.a) direction.x -= 1;
    if (state.keys.d) direction.x += 1;
    if (state.keys.space) direction.y += 1;
    if (state.keys.shift) direction.y -= 1;

    if (direction.length() > 0) {
        direction.normalize().multiplyScalar(speed);
        direction.applyQuaternion(state.camera.quaternion);
        state.camera.position.add(direction);
        state.controls.target.copy(state.camera.position).add(new THREE.Vector3(0, 0, -100).applyQuaternion(state.camera.quaternion));
    }
}

export function handleDeleteClick(event) {
    if (!state.deleteMode) return;
    if (!state.renderer || !state.camera) return;

    const rect = state.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1
    );

    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(mouse, state.camera);

    const allMeshes = [];
    state.loadedModels.forEach(model => {
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
        let foundContainer = null;
        let curr = hitMesh;

        while (curr && curr !== state.scene) {
            // Check if this node is the one with the metadata (the Actor Node from objects.gltf)
            if (curr.userData && (curr.userData.mesh_ref || curr.userData.is_prefab)) {
                foundContainer = curr;
                break;
            }

            // If we hit a root loaded model (the Scene root), we stop just below it
            if (state.loadedModels.includes(curr.parent)) {
                // The current node is a direct child of a loaded root (e.g. an object node)
                if (!foundContainer) foundContainer = curr;
                break;
            }

            curr = curr.parent;
        }

        if (foundContainer) {
            target = foundContainer;
        } else {
            // Fallback to original behavior
            while (target.parent && target.parent !== state.scene && !state.loadedModels.includes(target)) {
                target = target.parent;
            }
        }

        const meshName = target.userData?.mesh_ref || target.name || hitMesh.name || 'unnamed';
        console.log('--- DELETING MESH ---');
        console.log('Target Name:', target.name);
        console.log('Mesh Ref:', target.userData?.mesh_ref);

        state.deletedMeshNames.add(meshName);
        console.log('Current Deleted List:', Array.from(state.deletedMeshNames));

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
        console.log(`Successfully deleted: ${meshName}. Total deleted meshes: ${state.deletedMeshNames.size}`);
    }
}

export function toggleFlyMode() {
    state.flyMode = !state.flyMode;
    const btn = document.getElementById('fly-mode-btn');
    if (btn) {
        btn.textContent = state.flyMode ? 'Fly Mode ON' : 'Fly Mode';
        btn.classList.toggle('active', state.flyMode);
    }
    if (state.controls) state.controls.enabled = !state.flyMode;
}

export function toggleDeleteMode() {
    state.deleteMode = !state.deleteMode;
    const btn = document.getElementById('delete-mode-btn');
    if (btn) {
        btn.textContent = state.deleteMode ? 'DELETE MODE ON' : 'Delete Mode';
        btn.classList.toggle('active', state.deleteMode);
    }
    document.body.classList.toggle('delete-mode', state.deleteMode);
    console.log('Delete mode:', state.deleteMode ? 'ON' : 'OFF');
}
