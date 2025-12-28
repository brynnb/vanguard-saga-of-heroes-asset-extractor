import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { state } from './state.js';
import { CONFIG } from './config.js';
import { setupEventListeners, updateFlyMode } from './input.js';
import { loadMeshList, loadChunkList } from './ui.js';

export function init() {
    // Scene
    state.scene = new THREE.Scene();
    state.scene.background = new THREE.Color(0x1a1a2e);

    // Camera
    state.camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 1, CONFIG.cameraFar);
    state.camera.position.set(200, 200, 200);

    // Renderer
    state.renderer = new THREE.WebGLRenderer({ antialias: true });
    state.renderer.setSize(window.innerWidth, window.innerHeight);
    state.renderer.setPixelRatio(window.devicePixelRatio);
    state.renderer.shadowMap.enabled = true;
    document.getElementById('container').appendChild(state.renderer.domElement);

    // Controls
    state.controls = new OrbitControls(state.camera, state.renderer.domElement);
    state.controls.enableDamping = true;
    state.controls.dampingFactor = 0.05;
    state.controls.screenSpacePanning = true;
    state.controls.maxDistance = CONFIG.cameraFar;

    // Lighting
    setupLighting();

    // Grid
    state.gridHelper = new THREE.GridHelper(CONFIG.gridSize, 200, 0x444444, 0x222222);
    state.scene.add(state.gridHelper);

    // Event Listeners
    setupEventListeners();

    // Load mesh and BSP lists
    loadMeshList();
    loadChunkList();

    // Start render loop
    animate();

    console.log('Vanguard Mesh Viewer initialized');
}

export function setupLighting() {
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
    state.scene.add(ambientLight);

    const directionalLight = new THREE.DirectionalLight(0xffffff, 1.0);
    directionalLight.position.set(500, 1000, 500);
    directionalLight.castShadow = true;
    directionalLight.shadow.mapSize.width = 2048;
    directionalLight.shadow.mapSize.height = 2048;
    state.scene.add(directionalLight);

    const sunLight = new THREE.DirectionalLight(0xfff9e8, 2.5);
    sunLight.position.set(0, 1000, 0);
    sunLight.castShadow = true;
    state.scene.add(sunLight);

    const fillLight = new THREE.DirectionalLight(0x8888ff, 0.3);
    fillLight.position.set(-200, 100, -200);
    state.scene.add(fillLight);
}

export function animate() {
    requestAnimationFrame(animate);

    if (state.flyMode) {
        updateFlyMode();
    } else {
        state.controls.update();
    }

    if (state.renderer && state.scene && state.camera) {
        state.renderer.render(state.scene, state.camera);
    }
}

export function resetCamera() {
    state.camera.position.set(200, 200, 200);
    state.controls.target.set(0, 0, 0);
    state.controls.update();
}

export function toggleWireframe() {
    state.wireframeMode = !state.wireframeMode;
    state.loadedModels.forEach(model => {
        model.traverse(child => {
            if (child.isMesh && child.material) {
                const mats = Array.isArray(child.material) ? child.material : [child.material];
                mats.forEach(mat => { mat.wireframe = state.wireframeMode; });
            }
        });
    });
}

export function toggleGrid() {
    state.gridVisible = !state.gridVisible;
    if (state.gridHelper) state.gridHelper.visible = state.gridVisible;
}
