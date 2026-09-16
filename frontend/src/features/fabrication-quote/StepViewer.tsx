import React, { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { objectValue } from './SourcesPanel';

/** Display-only meshes. Costing always uses backend B-Rep measurements. */
export default function StepViewer({ geometry }: { geometry: Record<string, unknown> }) {
  const container = useRef<HTMLDivElement>(null); const highlight = useRef<(id: string | null) => void>(() => undefined);
  const fit = useRef<() => void>(() => undefined); const [selected, setSelected] = useState<string | null>(null); const [error, setError] = useState('');
  const occurrences = (Array.isArray(geometry.occurrences) ? geometry.occurrences : []).map(objectValue);
  useEffect(() => {
    const host = container.current; if (!host) return;
    let renderer: THREE.WebGLRenderer | null = null; let controls: OrbitControls | null = null; let animation = 0; let resize: ResizeObserver | null = null;
    const geometries: THREE.BufferGeometry[] = []; const materials: THREE.MeshStandardMaterial[] = []; const objects: THREE.Mesh[] = [];
    let cleanEvents: () => void = () => undefined;
    try {
      const theme = getComputedStyle(host);
      const themeColor = (token: string, fallback: string) => new THREE.Color(theme.getPropertyValue(token).trim() || fallback);
      const partColors = [themeColor('--fd-body-2', '#cbd5e1'), themeColor('--fd-cyan', '#39c5cf'), themeColor('--fd-link', '#93c5fd'), themeColor('--fd-mute', '#8a98ab'), themeColor('--fd-blue', '#2f81f7')];
      const selectedColor = themeColor('--fd-blue', '#2f81f7');
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2)); renderer.setClearColor(themeColor('--fd-sunken', '#0a0e15')); host.appendChild(renderer.domElement);
      renderer.domElement.setAttribute('aria-label', 'STEP model. Drag to orbit, scroll to zoom. Select an occurrence in the adjacent list.');
      const scene = new THREE.Scene(); scene.add(new THREE.HemisphereLight(0xffffff, themeColor('--fd-raised', '#1b2330'), 2.2)); const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(3, -4, 6); scene.add(light);
      const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 100000); camera.up.set(0, 0, 1); controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;
      const meshData = (Array.isArray(geometry.meshes) ? geometry.meshes : []).map(objectValue); const definitions = new Map<string, THREE.BufferGeometry>();
      meshData.forEach(data => {
        if (!Array.isArray(data.positions) || !Array.isArray(data.indices) || data.positions.length % 3 || !data.positions.every(n => typeof n === 'number' && Number.isFinite(n)) || !data.indices.every(n => typeof n === 'number' && Number.isInteger(n) && n >= 0 && n < (data.positions as unknown[]).length / 3)) return;
        const shape = new THREE.BufferGeometry(); shape.setAttribute('position', new THREE.Float32BufferAttribute(data.positions, 3)); shape.setIndex(data.indices); shape.computeVertexNormals(); geometries.push(shape); definitions.set(String(data.id), shape);
      });
      const nodes = (Array.isArray(geometry.occurrences) ? geometry.occurrences : []).map(objectValue);
      nodes.filter(node => !node.is_assembly).forEach((node, index) => {
        const shape = definitions.get(String(node.definition_id)); const values = Array.isArray(node.transform_mm) ? node.transform_mm.flat() : [];
        if (!shape || values.length !== 16 || !values.every(n => typeof n === 'number' && Number.isFinite(n))) return;
        const material = new THREE.MeshStandardMaterial({ color: partColors[index % partColors.length], metalness: 0.2, roughness: 0.58, side: THREE.DoubleSide }); materials.push(material);
        const mesh = new THREE.Mesh(shape, material); mesh.name = String(node.id); mesh.matrixAutoUpdate = false; mesh.matrix.fromArray(values).transpose(); scene.add(mesh); objects.push(mesh);
      });
      if (!objects.length) throw new Error('No supported display meshes are available. Review the occurrence list and original STEP file.');
      scene.updateMatrixWorld(true); const bounds = new THREE.Box3().setFromObject(scene); const center = bounds.getCenter(new THREE.Vector3()); const radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.1);
      const orbit = controls; fit.current = () => { camera.position.copy(center).add(new THREE.Vector3(radius * 1.5, -radius * 1.8, radius * 1.4)); camera.near = radius / 1000; camera.far = radius * 1000; camera.updateProjectionMatrix(); orbit.target.copy(center); orbit.update(); }; fit.current();
      highlight.current = id => objects.forEach(mesh => { const material = mesh.material as THREE.MeshStandardMaterial; material.emissive.copy(selectedColor); material.emissiveIntensity = id === mesh.name ? 0.65 : 0; });
      const canvas = renderer.domElement; const raycaster = new THREE.Raycaster(); let down = { x: 0, y: 0 };
      const pointerDown = (event: PointerEvent) => { down = { x: event.clientX, y: event.clientY }; };
      const pointerUp = (event: PointerEvent) => { if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5) return; const rect = canvas.getBoundingClientRect(); raycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera); setSelected(raycaster.intersectObjects(objects, false)[0]?.object.name || null); };
      canvas.addEventListener('pointerdown', pointerDown); canvas.addEventListener('pointerup', pointerUp); cleanEvents = () => { canvas.removeEventListener('pointerdown', pointerDown); canvas.removeEventListener('pointerup', pointerUp); };
      const size = () => { const width = Math.max(host.clientWidth, 1); const height = 340; renderer?.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix(); };
      size(); if (typeof ResizeObserver !== 'undefined') { resize = new ResizeObserver(size); resize.observe(host); }
      const draw = () => { orbit.update(); renderer?.render(scene, camera); animation = requestAnimationFrame(draw); }; draw();
    } catch (e) { setError(e instanceof Error ? e.message : '3D preview unavailable.'); }
    return () => { cancelAnimationFrame(animation); resize?.disconnect(); cleanEvents(); controls?.dispose(); geometries.forEach(g => g.dispose()); materials.forEach(m => m.dispose()); renderer?.dispose(); renderer?.domElement.remove(); highlight.current = () => undefined; fit.current = () => undefined; };
  }, [geometry]);
  useEffect(() => { highlight.current(selected); }, [selected]);
  return <div className="fq-model-review"><div className="fq-model-toolbar"><strong>Assembly inspection</strong><button type="button" className="fq-text-button" onClick={() => fit.current()}>Fit model</button></div>{error && <p className="fq-alert">{error}</p>}<div className="fq-model-layout"><div className="fq-model-canvas" ref={container} /><div className="fq-occurrences" aria-label="STEP assembly occurrences">{occurrences.map((node, i) => <button type="button" key={String(node.id || i)} aria-pressed={selected === node.id} onClick={() => setSelected(String(node.id))} style={{ paddingLeft: 9 + Math.min(String(node.id).split('/').length - 1, 5) * 9 }}><span>{node.is_assembly ? '▧' : '◇'}</span><div>{String(node.name || node.definition_id || node.id)}<small>{String(node.id)}</small></div></button>)}</div></div><p className="fq-hint">Drag to orbit · scroll to zoom · click to select. Display tessellation is approximate; source B-Rep geometry controls measurements. {selected && `Selected: ${selected}`}</p></div>;
}
