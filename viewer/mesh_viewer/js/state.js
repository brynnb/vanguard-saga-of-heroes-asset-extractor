import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

export const state = {
    // Three.js Core
    scene: null,
    camera: null,
    renderer: null,
    controls: null,

    // Lighting
    directionalLight: null,
    ambientLight: null,
    gridHelper: null,

    // App Mode
    flyMode: true,
    deleteMode: false,
    wireframeMode: false,
    gridVisible: true,

    // Input States
    keys: { w: false, a: false, s: false, d: false, space: false, shift: false },
    yaw: 0,
    pitch: 0,
    euler: new THREE.Euler(0, 0, 0, 'YXZ'),

    // Data Lists
    meshList: [],
    filteredMeshList: [],
    chunkList: [],
    filteredChunkList: [],
    loadedModels: [],

    // Caches & Sets
    meshCache: new Map(),
    deletedMeshNames: new Set(),
    chunkMeshRefs: new Set(),

    // State
    currentChunk: null,

    // Loaders
    gltfLoader: new GLTFLoader()
};
